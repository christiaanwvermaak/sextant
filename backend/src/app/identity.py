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

from . import providers as providers_mod


class AuthError(Exception):
    """Message is safe to show a user."""


# Token verification lives in providers.py, which discovers each provider's own
# jwks_uri rather than assuming Keycloak's path. See the note at the top of that
# file — the hard-coded "/protocol/openid-connect/certs" was the bug.


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

    # A local token is `user:expiry:mac` and never a JWT, which always has two
    # dots. Checking the shape first avoids handing a local token to a JWKS
    # client and getting a confusing signing-key error back.
    if token.count(".") < 2:
        return _from_local_token(config.auth, token)

    # Read `iss` unverified ONLY to choose which provider verifies it. A forged
    # issuer just means verification then fails against that provider's keys.
    issuer = providers_mod.issuer_of(token)
    provider = config.auth.provider_for_issuer(issuer)
    if provider is None:
        # Naming the issuer helps whoever configured it and tells an attacker
        # nothing they did not already put in the token themselves.
        raise AuthError(
            f"no configured identity provider issues tokens for {issuer or 'that token'}")
    try:
        return provider.verify(token)
    except providers_mod.ProviderError as exc:
        raise AuthError(str(exc)) from None


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
