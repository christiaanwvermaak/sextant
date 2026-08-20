"""The guarantees that make this safe to point at a production database.

These test refusals and the audit contract, not MongoDB. A live database is not
needed to prove that a read-only connection refuses a write, and a test that
needs one would not be run.
"""
import json
import os

import pytest
import yaml

from src.app import audit as audit_mod
from src.app import config as config_mod
from src.app import mutations


# ── configuration ──────────────────────────────────────────────────────────

def write_config(tmp_path, **overrides):
    raw = {
        "connections": [{
            "id": "local", "name": "Local", "uri": "mongodb://u:p@localhost:27017",
            **overrides,
        }],
        "auth": {"local": {"username": "operator", "password_env": "TESTPW"}},
    }
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_a_connection_is_read_only_unless_it_says_otherwise(tmp_path, monkeypatch):
    """The default must be the safe one. Someone will point this at production
    before they finish reading the documentation."""
    monkeypatch.setenv("TESTPW", "x")
    cfg = config_mod.load(str(write_config(tmp_path)))
    assert cfg.connection("local").writable is False


def test_writable_connections_require_confirmation_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPW", "x")
    cfg = config_mod.load(str(write_config(tmp_path, writable=True)))
    assert cfg.connection("local").confirm_writes is True


def test_refuses_to_start_with_no_way_to_sign_in(tmp_path):
    raw = {"connections": [{"id": "a", "uri": "mongodb://h"}], "auth": {}}
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(str(p))
    assert "sign in" in str(exc.value)


def test_a_missing_env_var_fails_at_boot_not_at_connect_time(tmp_path, monkeypatch):
    """A half-substituted URI would otherwise surface as an authentication
    failure hours later, against the wrong database."""
    monkeypatch.setenv("TESTPW", "x")
    monkeypatch.delenv("NOPE", raising=False)
    raw = {
        "connections": [{"id": "a", "uri": "mongodb://u:${NOPE}@h"}],
        "auth": {"local": {"username": "o", "password_env": "TESTPW"}},
    }
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(str(p))
    assert "NOPE" in str(exc.value)


def test_the_uri_is_never_serialised_to_a_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPW", "x")
    cfg = config_mod.load(str(write_config(tmp_path)))
    public = cfg.connection("local").public()
    assert "uri" not in public
    assert "p@localhost" not in json.dumps(public)


# ── refusals ───────────────────────────────────────────────────────────────

class FakeConn:
    def __init__(self, writable=True, confirm_writes=True):
        self.id = "c"; self.name = "Conn"
        self.writable = writable; self.confirm_writes = confirm_writes


def test_a_read_only_connection_refuses_every_write():
    conn = FakeConn(writable=False)
    with pytest.raises(mutations.Refused) as exc:
        mutations._check(conn, confirm=True)
    assert "read-only" in str(exc.value)


def test_a_confirm_connection_refuses_an_unconfirmed_write():
    with pytest.raises(mutations.Refused) as exc:
        mutations._check(FakeConn(), confirm=False)
    assert "confirmation" in str(exc.value)


def test_confirmation_is_enough_when_the_connection_allows_writing():
    mutations._check(FakeConn(), confirm=True)   # must not raise


# ── the audit contract ─────────────────────────────────────────────────────

def test_a_change_is_not_reported_as_done_if_it_could_not_be_recorded(tmp_path):
    """The whole point. If the audit write fails the caller must hear about it."""
    a = audit_mod.Audit(str(tmp_path / "sub" / "audit.log"))
    huge = {"blob": "x" * (audit_mod.MAX_PREIMAGE_BYTES + 10)}
    with pytest.raises(audit_mod.AuditError):
        a.record(who="w", action="delete", connection="c", database="d",
                 collection="col", pre_image=huge)


def test_the_pre_image_is_what_makes_an_undo_possible(tmp_path):
    a = audit_mod.Audit(str(tmp_path / "audit.log"))
    a.record(who="wimpie", action="delete", connection="c", database="d",
             collection="people", pre_image={"_id": 1, "name": "Chanelle"})
    entry = a.tail()[0]
    assert entry["pre_image"] == {"_id": 1, "name": "Chanelle"}
    assert entry["who"] == "wimpie"


def test_a_failed_change_is_still_recorded(tmp_path):
    """A silent failure and a silent success look identical afterwards."""
    a = audit_mod.Audit(str(tmp_path / "audit.log"))
    with pytest.raises(RuntimeError):
        with audit_mod.guard(a, who="w", action="replace", connection="c",
                             database="d", collection="col") as g:
            g.pre_image = {"_id": 1}
            raise RuntimeError("driver blew up")
    entry = a.tail()[0]
    assert entry["action"] == "replace:FAILED"
    assert entry["pre_image"] == {"_id": 1}


def test_bson_types_do_not_break_the_audit_writer(tmp_path):
    import datetime
    from bson import ObjectId
    a = audit_mod.Audit(str(tmp_path / "audit.log"))
    a.record(who="w", action="insert", connection="c", database="d",
             collection="col",
             post_image={"_id": ObjectId(), "when": datetime.datetime.now(datetime.UTC)})
    assert len(a.tail()) == 1


def test_entries_are_newest_first(tmp_path):
    a = audit_mod.Audit(str(tmp_path / "audit.log"))
    for i in range(3):
        a.record(who="w", action=f"a{i}", connection="c", database="d", collection="col")
    assert [e["action"] for e in a.tail()] == ["a2", "a1", "a0"]


# ── the local session token ────────────────────────────────────────────────
#
# Added when the first draft had the frontend forward a trusted username header.
# That works only while nothing else can reach the backend, and "not published"
# is a deployment detail rather than authentication.

from src.app import identity  # noqa: E402


def test_a_valid_local_token_identifies_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPW", "hunter2")
    cfg = config_mod.load(str(write_config(tmp_path)))
    who = identity.sign_in_local(cfg, "operator", "hunter2")
    token = identity.issue_local_token(cfg.auth, who)
    back = identity._from_local_token(cfg.auth, token)
    assert back["username"] == "operator"
    assert back["groups"] == []          # break-glass carries no groups, ever


def test_a_tampered_token_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPW", "hunter2")
    cfg = config_mod.load(str(write_config(tmp_path)))
    who = identity.sign_in_local(cfg, "operator", "hunter2")
    token = identity.issue_local_token(cfg.auth, who)

    user, expires, mac = token.rsplit(":", 2)
    for forged in (f"root:{expires}:{mac}",            # different user
                   f"{user}:{int(expires)+99999}:{mac}",  # extended lifetime
                   f"{user}:{expires}:{'0'*len(mac)}"):   # invented signature
        with pytest.raises(identity.AuthError):
            identity._from_local_token(cfg.auth, forged)


def test_an_expired_token_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPW", "hunter2")
    cfg = config_mod.load(str(write_config(tmp_path)))
    who = identity.sign_in_local(cfg, "operator", "hunter2")
    token = identity.issue_local_token(cfg.auth, who, now=0)   # issued in 1970
    with pytest.raises(identity.AuthError) as exc:
        identity._from_local_token(cfg.auth, token)
    assert "expired" in str(exc.value)


def test_the_token_is_not_the_password(tmp_path, monkeypatch):
    """A leaked token must not hand back the credential that signs it."""
    monkeypatch.setenv("TESTPW", "hunter2")
    cfg = config_mod.load(str(write_config(tmp_path)))
    who = identity.sign_in_local(cfg, "operator", "hunter2")
    token = identity.issue_local_token(cfg.auth, who)
    assert "hunter2" not in token


def test_changing_the_configured_username_invalidates_old_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPW", "hunter2")
    cfg = config_mod.load(str(write_config(tmp_path)))
    who = identity.sign_in_local(cfg, "operator", "hunter2")
    token = identity.issue_local_token(cfg.auth, who)
    cfg.auth.local_user = "someone-else"
    with pytest.raises(identity.AuthError):
        identity._from_local_token(cfg.auth, token)
