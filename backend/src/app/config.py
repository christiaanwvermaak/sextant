"""Configuration: connections, sign-in, and what may be changed.

**Nothing about any particular estate is compiled in.** Point this at a MongoDB
and it discovers the databases and collections that the credential you gave it
can see. Everything site-specific lives in one YAML file supplied at deploy time.

The shape is deliberately close to how Compass thinks: a *connection* is a
connection string plus a display name. What you can do through it is what that
credential is allowed to do — this tool does not invent privileges the database
would not grant.

What it adds on top is the part a desktop app cannot: **who is allowed to reach
the connection at all**, and **a record of what was changed**.
"""
import os
import re

import yaml

from . import providers as providers_mod

CONFIG_PATH = os.environ.get("SEXTANT_CONFIG", "/config/sextant.yml")

# Substitutes ${ENV_VAR} so a connection string with a password can be assembled
# from a Kubernetes secret rather than written into the config file.
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Configuration is unusable. Raised at boot, never mid-request."""


def _expand(value):
    if not isinstance(value, str):
        return value

    def sub(m):
        name = m.group(1)
        if name not in os.environ:
            raise ConfigError(
                f"config references ${{{name}}} but that variable is not set. "
                "Refusing to start with a half-built connection string."
            )
        return os.environ[name]

    return _ENV.sub(sub, value)


def _walk(node):
    if isinstance(node, dict):
        return {k: _walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v) for v in node]
    return _expand(node)


class Connection:
    """One MongoDB this console can reach."""

    def __init__(self, raw):
        self.id = raw["id"]
        self.name = raw.get("name", raw["id"])
        self.uri = raw["uri"]
        # Writes are OFF unless the config says otherwise. A tool that defaults
        # to letting people edit production is a tool that will eventually be
        # pointed at production by someone who did not think about it.
        self.writable = bool(raw.get("writable", False))
        # Extra friction for the connections that deserve it. A write here must
        # carry an explicit confirmation the UI cannot send by accident.
        self.confirm_writes = bool(raw.get("confirm_writes", self.writable))
        # Optional: only these identity-provider groups may use this connection.
        # Empty means anyone who can sign in.
        self.allowed_groups = tuple(raw.get("allowed_groups", ()) or ())
        # Optional: restrict to named databases. Empty means whatever the
        # credential can see, which is Compass's behaviour.
        self.databases = tuple(raw.get("databases", ()) or ())

        if not self.uri.startswith(("mongodb://", "mongodb+srv://")):
            raise ConfigError(f"connection '{self.id}': uri must be a mongodb:// or mongodb+srv:// URI")

    def visible_to(self, groups):
        if not self.allowed_groups:
            return True
        return bool(set(self.allowed_groups) & set(groups or ()))

    def allows_database(self, database):
        return not self.databases or database in self.databases

    def public(self, groups=()):
        """Safe to serialise to a browser. **Never includes the URI**, which
        carries the password."""
        return {
            "id": self.id,
            "name": self.name,
            "writable": self.writable,
            "confirm_writes": self.confirm_writes,
            "readable": self.visible_to(groups),
        }


class Auth:
    """How people sign in.

    A LIST of providers, not one. Sextant is meant to sit in front of whatever
    identity you already run — Keycloak here, WeldForge there, both at once
    during a migration — and a single hard-coded block makes that impossible.

    Plus a local break-glass credential, on purpose: OIDC against a provider you
    already run is the everyday path, and a local login is the way in when that
    provider is itself the outage. A database console that cannot be opened
    during an identity incident is one you cannot use on the day you need it.
    """

    def __init__(self, raw):
        raw = raw or {}

        entries = raw.get("providers") or []
        # Accept the older single-block form so an existing config keeps working
        # rather than failing at boot after an upgrade.
        if not entries and raw.get("oidc"):
            legacy = dict(raw["oidc"])
            legacy.setdefault("id", "oidc")
            legacy.setdefault("name", "Single sign-on")
            entries = [legacy]

        self.providers = {}
        self.by_issuer = {}
        for entry in entries:
            provider = providers_mod.Provider(entry)
            if provider.id in self.providers:
                raise ConfigError(f"duplicate provider id '{provider.id}'")
            if provider.issuer in self.by_issuer:
                raise ConfigError(
                    f"providers '{self.by_issuer[provider.issuer].id}' and "
                    f"'{provider.id}' share the issuer {provider.issuer}. A token "
                    "could not be attributed to one of them."
                )
            self.providers[provider.id] = provider
            self.by_issuer[provider.issuer] = provider

        local = raw.get("local") or {}
        self.local_user = local.get("username")
        self.local_password_env = local.get("password_env", "SEXTANT_PASSWORD")
        self.local_enabled = bool(self.local_user and os.environ.get(self.local_password_env))

        # Starting with neither configured would serve an open console over
        # whatever databases the config names. Refuse instead.
        if not self.providers and not self.local_enabled:
            raise ConfigError(
                "no way to sign in is configured. Add at least one entry under "
                "auth.providers, or auth.local.username plus its password "
                "environment variable. Refusing to start an unauthenticated console."
            )

    def provider_for_issuer(self, issuer):
        return self.by_issuer.get((issuer or "").rstrip("/"))

    def public(self):
        """What the sign-in screen needs. Never a secret."""
        return {
            "providers": [p.public() for p in self.providers.values()],
            "local": bool(self.local_enabled),
        }


class Config:
    def __init__(self, raw):
        conns = raw.get("connections") or []
        if not conns:
            raise ConfigError("no connections configured")
        self.connections = {}
        for c in conns:
            conn = Connection(c)
            if conn.id in self.connections:
                raise ConfigError(f"duplicate connection id '{conn.id}'")
            self.connections[conn.id] = conn
        self.auth = Auth(raw.get("auth"))
        self.audit_path = raw.get("audit", {}).get("path", "/data/audit.log")
        self.max_documents = int(raw.get("limits", {}).get("max_documents", 200))
        self.max_time_ms = int(raw.get("limits", {}).get("max_time_ms", 10_000))

    def connection(self, connection_id):
        conn = self.connections.get(connection_id)
        if conn is None:
            raise ConfigError(f"unknown connection '{connection_id}'")
        return conn


_cached = None


def load(path=None):
    """Read the config once. Raises at boot rather than per request."""
    global _cached
    if _cached is not None and path is None:
        return _cached
    target = path or CONFIG_PATH
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"no config at {target}. Set SEXTANT_CONFIG or mount one there. "
            "See examples/sextant.yml."
        ) from None
    cfg = Config(_walk(raw))
    if path is None:
        _cached = cfg
    return cfg
