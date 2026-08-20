"""The routes must enforce the boundary, not merely have one available.

`mutations` can hold the strictest rules in the world and it changes nothing
unless the handlers go through them. These drive the real handlers.

There is a second trap these pin down. Tina4 defaults
`auth_required = method not in ("GET","HEAD","OPTIONS","ANY")`, so every POST is
guarded by Tina4's own bearer scheme keyed on TINA4_SECRET — unrelated to our
OIDC token and impossible for it to satisfy. A write route that forgets
`auth_required=False` never reaches its handler and returns a bare
"Unauthorized", which looks like a permissions bug rather than a wiring bug.
"""
import inspect
import re

import pytest
import yaml

from src.app import config as config_mod
from src.app import identity, mutations


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SEXTANT_PASSWORD", "hunter2")
    raw = {
        "connections": [
            {"id": "ro", "name": "Read only", "uri": "mongodb://h:27017"},
            {"id": "rw", "name": "Writable", "uri": "mongodb://h:27017",
             "writable": True, "confirm_writes": True},
            {"id": "secret", "name": "Restricted", "uri": "mongodb://h:27017",
             "allowed_groups": ["db-admins"]},
        ],
        "auth": {"local": {"username": "operator", "password_env": "SEXTANT_PASSWORD"}},
        "audit": {"path": str(tmp_path / "audit.log")},
    }
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return config_mod.load(str(p))


# ── the connection boundary ────────────────────────────────────────────────

def test_a_restricted_connection_is_invisible_without_the_group(cfg):
    who = {"username": "someone", "groups": [], "via": "local"}
    ids = [c["id"] for c in identity.visible_connections(cfg, who)]
    assert "secret" not in ids
    assert {"ro", "rw"} <= set(ids)


def test_the_group_grants_it(cfg):
    who = {"username": "someone", "groups": ["db-admins"], "via": "oidc"}
    assert "secret" in [c["id"] for c in identity.visible_connections(cfg, who)]


def test_asking_for_a_connection_you_may_not_see_is_refused(cfg):
    who = {"username": "someone", "groups": [], "via": "local"}
    with pytest.raises(identity.AuthError):
        identity.connection_for(cfg, who, "secret")


def test_the_refusal_does_not_reveal_whether_it_exists(cfg):
    """Otherwise the error message enumerates the config."""
    who = {"username": "someone", "groups": [], "via": "local"}
    real, invented = None, None
    try:
        identity.connection_for(cfg, who, "secret")
    except identity.AuthError as exc:
        real = str(exc)
    try:
        identity.connection_for(cfg, who, "no-such-connection")
    except identity.AuthError as exc:
        invented = str(exc)
    assert real == invented


def test_break_glass_cannot_reach_a_group_restricted_connection(cfg):
    """The emergency account must not inherit the most privileged access."""
    who = identity.sign_in_local(cfg, "operator", "hunter2")
    assert who["groups"] == []
    with pytest.raises(identity.AuthError):
        identity.connection_for(cfg, who, "secret")


def test_a_wrong_local_password_is_refused(cfg):
    with pytest.raises(identity.AuthError):
        identity.sign_in_local(cfg, "operator", "wrong")


def test_a_wrong_local_username_is_refused(cfg):
    with pytest.raises(identity.AuthError):
        identity.sign_in_local(cfg, "root", "hunter2")


# ── writes go through the guard ────────────────────────────────────────────

def test_the_read_only_connection_refuses_writes(cfg):
    with pytest.raises(mutations.Refused):
        mutations._check(cfg.connection("ro"), confirm=True)


def test_the_writable_connection_still_needs_confirmation(cfg):
    with pytest.raises(mutations.Refused):
        mutations._check(cfg.connection("rw"), confirm=False)
    mutations._check(cfg.connection("rw"), confirm=True)


# ── wiring ─────────────────────────────────────────────────────────────────

def test_every_write_route_opts_out_of_tina4s_own_auth_guard():
    """Any POST/DELETE handler missing auth_required=False is unreachable."""
    source = open("src/routes/api.py", encoding="utf-8").read()
    decorated = re.findall(r"@(post|delete)\(([^)]*)\)", source, re.S)
    assert decorated, "no write routes found — has the file moved?"
    missing = [d for verb, d in decorated if "auth_required=False" not in d]
    assert not missing, (
        "these write routes will never reach their handler:\n  " + "\n  ".join(missing))


def test_that_guard_would_actually_catch_a_regression():
    """Proves the check above is not vacuous."""
    sample = '@post("/api/x")\nasync def x(): pass'
    decorated = re.findall(r"@(post|delete)\(([^)]*)\)", sample, re.S)
    assert [d for verb, d in decorated if "auth_required=False" not in d]


def test_no_write_route_bypasses_the_mutations_module():
    """A handler that calls pymongo directly would skip the audit entirely."""
    source = open("src/routes/api.py", encoding="utf-8").read()
    for banned in ("insert_one", "update_one", "replace_one", "delete_one", "delete_many("):
        # delete_many( is allowed only as mutations.delete_many
        occurrences = [
            m for m in re.finditer(re.escape(banned), source)
            if "mutations." not in source[max(0, m.start() - 12):m.start()]
        ]
        assert not occurrences, f"{banned} called outside mutations.py — that skips the audit"


def test_the_config_is_never_serialised_whole(cfg):
    """Only Connection.public() may reach a browser; it omits the URI."""
    who = {"username": "x", "groups": [], "via": "local"}
    blob = repr(identity.visible_connections(cfg, who))
    assert "mongodb://" not in blob
