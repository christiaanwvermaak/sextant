"""Identity providers, discovered rather than assumed.

**The mistake this file exists to correct.** The first version hard-coded
Keycloak's JWKS path:

    issuer + "/protocol/openid-connect/certs"

That is a Keycloak implementation detail, not part of OIDC. It works for exactly
one product and fails against every other with a signing-key error that reads
like a broken token. WeldForge, for instance, serves per-tenant endpoints under
`/t/{slug}/oauth2/jwks`, and Auth0, Entra and Okta each use a different path
again.

The spec already solved this: every provider serves
`{issuer}/.well-known/openid-configuration`, which names its own `jwks_uri`,
`authorization_endpoint` and the rest. Ask, do not guess.

**Claims differ too, and there is no standard.** Keycloak emits group membership
as `groups`; WeldForge emits `roles`. Neither is wrong. The claim name is
therefore per-provider configuration, not a constant.
"""
import json
import threading
import urllib.error
import urllib.request

import jwt
from jwt import PyJWKClient

DISCOVERY_TIMEOUT = 8


class ProviderError(Exception):
    """Message is safe to show a user."""


class Provider:
    """One identity provider Sextant will accept tokens from."""

    def __init__(self, raw):
        self.id = raw["id"]
        self.name = raw.get("name", raw["id"])
        self.kind = raw.get("type", "oidc")
        if self.kind != "oidc":
            raise ProviderError(
                f"provider '{self.id}': type '{self.kind}' is not supported. "
                "Every provider here speaks OIDC — including WeldForge, whose "
                "per-tenant issuer is https://<host>/t/<tenant>."
            )

        self.issuer = (raw.get("issuer") or "").rstrip("/")
        if not self.issuer:
            raise ProviderError(f"provider '{self.id}': issuer is required")

        self.client_id = raw.get("client_id")
        self.audience = raw.get("audience", self.client_id)

        # No standard exists for this. Keycloak: groups. WeldForge: roles.
        self.groups_claim = raw.get("groups_claim", "groups")
        self.username_claim = raw.get("username_claim")
        self.required_groups = tuple(raw.get("required_groups", ()) or ())

        # Some deployments sit behind a hostname that differs from the issuer in
        # the token (a cluster-internal Service, typically). Discovery then has
        # to be fetched from somewhere other than the issuer.
        self.discovery_url = raw.get("discovery_url") or (
            self.issuer + "/.well-known/openid-configuration")

        self._meta = None
        self._jwks = None
        self._lock = threading.Lock()

    # ── discovery ──────────────────────────────────────────────────────────

    def metadata(self):
        """Fetch and cache the provider's own description of itself."""
        if self._meta is not None:
            return self._meta
        with self._lock:
            if self._meta is not None:
                return self._meta
            try:
                request = urllib.request.Request(
                    self.discovery_url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(request, timeout=DISCOVERY_TIMEOUT) as fh:
                    self._meta = json.loads(fh.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raise ProviderError(
                    f"'{self.name}' returned HTTP {exc.code} for its OIDC discovery "
                    f"document at {self.discovery_url}. For WeldForge the issuer must "
                    "include the tenant, as https://<host>/t/<tenant>."
                ) from None
            except Exception as exc:  # noqa: BLE001 — urllib raises many shapes
                raise ProviderError(
                    f"could not reach '{self.name}' at {self.discovery_url}: {exc}"
                ) from None

            if "jwks_uri" not in self._meta:
                self._meta = None
                raise ProviderError(
                    f"'{self.name}' published a discovery document with no jwks_uri, "
                    "so its tokens cannot be verified."
                )
            return self._meta

    def jwks(self):
        if self._jwks is None:
            self._jwks = PyJWKClient(self.metadata()["jwks_uri"], cache_keys=True)
        return self._jwks

    def authorization_endpoint(self):
        return self.metadata().get("authorization_endpoint")

    def token_endpoint(self):
        return self.metadata().get("token_endpoint")

    def end_session_endpoint(self):
        return self.metadata().get("end_session_endpoint")

    def forget(self):
        """Drop cached discovery and keys. Called after a verification failure
        that looks like a rotation, so a rotated key recovers without a restart."""
        with self._lock:
            self._meta = None
            self._jwks = None

    # ── verification ───────────────────────────────────────────────────────

    def verify(self, token):
        """Verify a token from THIS provider and return the caller shape."""
        for attempt in (0, 1):
            try:
                key = self.jwks().get_signing_key_from_jwt(token)
                claims = jwt.decode(
                    token, key.key,
                    algorithms=self.metadata().get(
                        "id_token_signing_alg_values_supported", ["RS256"]),
                    audience=self.audience,
                    issuer=self.issuer,
                    options={"require": ["exp", "iat", "iss", "sub"],
                             "verify_aud": bool(self.audience)},
                )
                break
            except jwt.exceptions.PyJWKClientError:
                # Unknown key id is almost always a rotation. Refetch once, then
                # give up rather than hammering the provider on every request.
                if attempt:
                    raise ProviderError("the token signing key is not recognised") from None
                self.forget()
            except jwt.ExpiredSignatureError:
                raise ProviderError("your session has expired, please sign in again") from None
            except jwt.InvalidTokenError:
                # Deliberately not distinguishing bad signature from wrong
                # audience: that distinction helps someone probing more than it
                # helps a user.
                raise ProviderError("that token is not valid for this application") from None
        else:  # pragma: no cover
            raise ProviderError("the token could not be verified")

        groups = self.groups_of(claims)
        if self.required_groups and not (set(self.required_groups) & set(groups)):
            raise ProviderError(
                f"your account is not a member of a group permitted to use this console"
            )

        return {
            "username": self.username_of(claims),
            "groups": groups,
            "via": self.id,
            "provider": self.name,
        }

    def groups_of(self, claims):
        raw = claims.get(self.groups_claim) or []
        if isinstance(raw, str):
            raw = [raw]
        out = []
        for g in raw:
            if not isinstance(g, str):
                continue
            # Keycloak emits full paths as "/db-admins" when the mapper has
            # full-path on. Accepting both saves the operator guessing which
            # their realm is configured for.
            out.append(g[1:] if g.startswith("/") else g)
        return out

    def username_of(self, claims):
        if self.username_claim:
            return claims.get(self.username_claim) or claims.get("sub")
        # No standard here either. preferred_username is Keycloak's, email is
        # WeldForge's, sub always exists but is a UUID nobody recognises.
        for candidate in ("preferred_username", "email", "name", "sub"):
            if claims.get(candidate):
                return claims[candidate]
        return "unknown"

    def public(self):
        """Safe for a browser: never a client secret."""
        return {"id": self.id, "name": self.name, "kind": self.kind}


def issuer_of(token):
    """Read `iss` WITHOUT verifying, only to choose which provider to verify with.

    This is safe and is the standard approach: the unverified claim selects a key
    set, and a forged issuer simply means verification then fails. What would NOT
    be safe is trusting any other unverified claim, which is why nothing else is
    read here.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    iss = claims.get("iss")
    return iss.rstrip("/") if isinstance(iss, str) else None
