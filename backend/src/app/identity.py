"""Who is asking, and may they.

Two doors, matching the config: an OIDC bearer token, or a local break-glass
session. Both end up producing the same small shape, so nothing downstream has
to care which was used:

    {"username": ..., "groups": [...], "via": "oidc" | "local"}
"""
import hashlib
import hmac
import os
import time

import jwt
from jwt import PyJWKClient


class AuthError(Exception):
    """Message is safe to show a user."""


_jwks = {}


def _jwks_for(issuer):
    if issuer not in _jwks:
        _jwks[issuer] = PyJWKClient(
            issuer.rstrip("/") + "/protocol/openid-connect/certs",
            cache_keys=True,
        )
    return _jwks[issuer]


def bearer(request):
    header = ""
    try:
        headers = request.headers or {}
        header = headers.get("Authorization") or headers.get("authorization") or ""
    except AttributeError:
        header = ""
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


def _from_token(auth, token):
    """Verify signature, issuer and audience. Never trust an unverified claim."""
    for attempt in (0, 1):
        try:
            key = _jwks_for(auth.issuer).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"],
                audience=auth.audience, issuer=auth.issuer,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
            break
        except jwt.exceptions.PyJWKClientError:
            # Unknown key id is almost always a rotation. Refetch once.
            if attempt:
                raise AuthError("the token signing key is not recognised") from None
        except jwt.ExpiredSignatureError:
            raise AuthError("your session has expired, please sign in again") from None
        except jwt.InvalidTokenError:
            # Deliberately not distinguishing bad-signature from wrong-audience:
            # that distinction is more useful to someone probing than to a user.
            raise AuthError("that token is not valid for this application") from None
    else:  # pragma: no cover
        raise AuthError("the token could not be verified")

    groups = claims.get(auth.groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    # Full group paths arrive as "/db-admins" from some providers. Accept both
    # rather than making the operator guess which their realm emits.
    groups = [g[1:] if isinstance(g, str) and g.startswith("/") else g for g in groups]

    if auth.required_groups and not (set(auth.required_groups) & set(groups)):
        raise AuthError(
            "your account is not a member of a group permitted to use this console"
        )

    return {
        "username": claims.get("preferred_username") or claims.get("email") or claims.get("sub"),
        "groups": list(groups),
        "via": "oidc",
    }


def _from_local(auth, username, password):
    if not auth.local_enabled:
        raise AuthError("local sign-in is not configured")
    expected = os.environ.get(auth.local_password_env) or ""
    # compare_digest on both, so a wrong username costs the same time as a wrong
    # password and neither can be found by timing.
    ok_user = hmac.compare_digest((username or "").encode(), auth.local_user.encode())
    ok_pass = hmac.compare_digest((password or "").encode(), expected.encode())
    if not (ok_user and ok_pass):
        raise AuthError("that username and password do not match")
    # The break-glass account carries NO groups. It can therefore reach only
    # connections with no `allowed_groups` restriction -- an emergency login
    # should not silently inherit the most privileged access in the config.
    return {"username": auth.local_user, "groups": [], "via": "local"}


# ── the local session token ────────────────────────────────────────────────
#
# A signed token rather than a trusted header.
#
# The obvious shortcut is for the frontend proxy to forward the signed-in
# username in a header and have the backend believe it. That works right up
# until anything else can reach the backend -- another pod, a port-forward, a
# misconfigured Service -- at which point a header is a way to claim to be
# anybody. "The backend is not published" is a deployment detail, and
# authentication should not rest on one.
#
# So the backend signs a short-lived token at sign-in and verifies its own
# signature afterwards. The frontend only carries it.

_LOCAL_TTL = 8 * 3600


def _local_secret(auth):
    secret = os.environ.get(auth.local_password_env)
    if not secret:
        raise AuthError("local sign-in is not configured")
    # Derived rather than used directly, so the token key is not the password
    # itself -- a leaked token must not hand back the credential.
    return hashlib.sha256(("sextant-local-v1:" + secret).encode()).digest()


def issue_local_token(auth, who, now=None):
    now = int(now if now is not None else time.time())
    payload = f"{who['username']}:{now + _LOCAL_TTL}"
    mac = hmac.new(_local_secret(auth), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{mac}"


def _from_local_token(auth, token):
    try:
        username, expires, mac = token.rsplit(":", 2)
    except ValueError:
        raise AuthError("that session token is malformed") from None
    expected = hmac.new(_local_secret(auth), f"{username}:{expires}".encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise AuthError("that session token is not valid")
    try:
        if int(expires) < time.time():
            raise AuthError("your session has expired, please sign in again")
    except ValueError:
        raise AuthError("that session token is malformed") from None
    if username != auth.local_user:
        # The configured break-glass username changed since the token was
        # issued. Refuse rather than honouring a name that is no longer current.
        raise AuthError("that session token is no longer valid")
    return {"username": username, "groups": [], "via": "local"}


def identify(config, request):
    """Resolve the caller from the bearer token, or raise AuthError.

    ONE credential path: a bearer token. An OIDC token is verified against the
    issuer; a local token is verified against this backend's own signature. The
    caller cannot choose which -- the shape decides, and a forged one satisfies
    neither.
    """
    token = bearer(request)
    if not token:
        raise AuthError("not signed in")
    # A local token is `user:expiry:mac` and never a JWT, which has two dots and
    # a base64 header. Checking the shape avoids handing a local token to the
    # JWKS client and getting a confusing signing-key error.
    if token.count(".") >= 2 and config.auth.oidc_enabled:
        return _from_token(config.auth, token)
    return _from_local_token(config.auth, token)


def sign_in_local(config, username, password):
    return _from_local(config.auth, username, password)


def visible_connections(config, who):
    """The connections this caller may see, never including any URI."""
    groups = who.get("groups", [])
    return [
        c.public(groups) for c in config.connections.values()
        if c.visible_to(groups)
    ]


def connection_for(config, who, connection_id):
    """Resolve a connection the caller is actually entitled to.

    Raises AuthError rather than ConfigError for a connection that exists but is
    not theirs, so an unauthorised caller cannot use the error message to learn
    which connection ids are configured.
    """
    conn = config.connections.get(connection_id)
    if conn is None or not conn.visible_to(who.get("groups", [])):
        raise AuthError("that connection is not available to you")
    return conn
