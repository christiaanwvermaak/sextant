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

    Two doors, on purpose, mirroring the reasoning in Sentinel: OIDC against a
    provider you already run is the everyday path, and a local credential is the
    way in when that provider is itself the thing you are trying to fix. A
    database console that cannot be opened during an identity outage is a console
    you cannot use on the day you need it.
    """

    def __init__(self, raw):
        raw = raw or {}
        oidc = raw.get("oidc") or {}
        self.oidc_enabled = bool(oidc.get("issuer"))
        self.issuer = oidc.get("issuer")
        self.client_id = oidc.get("client_id")
        self.audience = oidc.get("audience", self.client_id)
        self.groups_claim = oidc.get("groups_claim", "groups")
        self.required_groups = tuple(oidc.get("required_groups", ()) or ())

        local = raw.get("local") or {}
        self.local_user = local.get("username")
        self.local_password_env = local.get("password_env", "SEXTANT_PASSWORD")
        self.local_enabled = bool(self.local_user and os.environ.get(self.local_password_env))

        # Starting with neither configured would serve an open console over
        # whatever databases the config names. Refuse instead.
        if not self.oidc_enabled and not self.local_enabled:
            raise ConfigError(
                "no way to sign in is configured. Set auth.oidc.issuer, or "
                "auth.local.username plus the password environment variable. "
                "Refusing to start an unauthenticated console."
            )


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
