"""Multiple identity providers, discovered rather than assumed.

The first version hard-coded Keycloak's JWKS path. These pin down that nothing
is Keycloak-specific any more, and that a token is attributed to exactly one
provider.
"""
import json
import pytest
import yaml

from src.app import config as config_mod
from src.app import providers as providers_mod


KEYCLOAK = "https://auth.example.com/realms/internal"
WELDFORGE = "https://weldforge.example.com/t/acme"


def write(tmp_path, auth):
    raw = {
        "connections": [{"id": "c", "uri": "mongodb://h:27017"}],
        "auth": auth,
        "audit": {"path": str(tmp_path / "a.log")},
    }
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(p)


def both():
    return {"providers": [
        {"id": "keycloak", "name": "CodeInfinity", "issuer": KEYCLOAK,
         "client_id": "sextant", "groups_claim": "groups"},
        # WeldForge emits `roles`, not `groups`. Neither is wrong; there is no
        # standard, which is exactly why it is configuration.
        {"id": "weldforge", "name": "WeldForge", "issuer": WELDFORGE,
         "client_id": "sextant", "groups_claim": "roles"},
    ]}


# ── configuration ──────────────────────────────────────────────────────────

def test_two_providers_can_be_configured_at_once(tmp_path):
    cfg = config_mod.load(write(tmp_path, both()))
    assert set(cfg.auth.providers) == {"keycloak", "weldforge"}


def test_a_token_is_attributed_to_its_issuer(tmp_path):
    cfg = config_mod.load(write(tmp_path, both()))
    assert cfg.auth.provider_for_issuer(KEYCLOAK).id == "keycloak"
    assert cfg.auth.provider_for_issuer(WELDFORGE).id == "weldforge"
    assert cfg.auth.provider_for_issuer("https://somewhere.else") is None


def test_a_trailing_slash_does_not_change_the_issuer(tmp_path):
    """Providers differ on whether they emit one. Neither should be a lockout."""
    cfg = config_mod.load(write(tmp_path, both()))
    assert cfg.auth.provider_for_issuer(KEYCLOAK + "/").id == "keycloak"


def test_two_providers_may_not_share_an_issuer(tmp_path):
    auth = {"providers": [
        {"id": "a", "issuer": KEYCLOAK, "client_id": "x"},
        {"id": "b", "issuer": KEYCLOAK + "/", "client_id": "y"},
    ]}
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(write(tmp_path, auth))
    assert "share the issuer" in str(exc.value)


def test_the_old_single_block_form_still_works(tmp_path):
    """An existing config must not fail at boot after an upgrade."""
    auth = {"oidc": {"issuer": KEYCLOAK, "client_id": "sextant"}}
    cfg = config_mod.load(write(tmp_path, auth))
    assert cfg.auth.provider_for_issuer(KEYCLOAK) is not None


def test_a_non_oidc_provider_type_is_refused_with_a_useful_message(tmp_path):
    auth = {"providers": [{"id": "x", "type": "saml", "issuer": KEYCLOAK}]}
    with pytest.raises(providers_mod.ProviderError) as exc:
        config_mod.load(write(tmp_path, auth))
    assert "WeldForge" in str(exc.value)   # tells you the right issuer shape


def test_no_client_secret_ever_reaches_the_browser(tmp_path):
    auth = both()
    auth["providers"][0]["client_secret"] = "s3cr3t"
    cfg = config_mod.load(write(tmp_path, auth))
    assert "s3cr3t" not in json.dumps(cfg.auth.public())


# ── nothing is Keycloak-specific ───────────────────────────────────────────

def test_the_jwks_url_comes_from_discovery_not_from_a_guess(tmp_path, monkeypatch):
    """The bug this replaced: `issuer + /protocol/openid-connect/certs`, which is
    a Keycloak path and wrong for every other provider."""
    cfg = config_mod.load(write(tmp_path, both()))
    wf = cfg.auth.providers["weldforge"]

    monkeypatch.setattr(wf, "_meta", {
        "issuer": WELDFORGE,
        "jwks_uri": WELDFORGE + "/oauth2/jwks",
        "authorization_endpoint": WELDFORGE + "/oauth2/authorize",
    })
    assert wf.metadata()["jwks_uri"] == WELDFORGE + "/oauth2/jwks"
    assert "protocol/openid-connect" not in wf.metadata()["jwks_uri"]


def test_the_discovery_url_defaults_to_the_spec_path(tmp_path):
    cfg = config_mod.load(write(tmp_path, both()))
    assert cfg.auth.providers["weldforge"].discovery_url == \
        WELDFORGE + "/.well-known/openid-configuration"


def test_discovery_can_be_pointed_elsewhere_than_the_issuer(tmp_path):
    """A cluster-internal Service often differs from the issuer in the token."""
    auth = {"providers": [{"id": "k", "issuer": KEYCLOAK, "client_id": "x",
                           "discovery_url": "http://keycloak.svc/realms/internal/.well-known/openid-configuration"}]}
    cfg = config_mod.load(write(tmp_path, auth))
    assert cfg.auth.providers["k"].discovery_url.startswith("http://keycloak.svc")


# ── claim mapping ──────────────────────────────────────────────────────────

def test_each_provider_reads_its_own_groups_claim(tmp_path):
    cfg = config_mod.load(write(tmp_path, both()))
    kc = cfg.auth.providers["keycloak"]
    wf = cfg.auth.providers["weldforge"]

    claims = {"groups": ["kc-group"], "roles": ["wf-role"]}
    assert kc.groups_of(claims) == ["kc-group"]
    assert wf.groups_of(claims) == ["wf-role"]


def test_a_leading_slash_on_a_group_is_tolerated(tmp_path):
    """Keycloak emits "/db-admins" when the mapper has full-path on."""
    cfg = config_mod.load(write(tmp_path, both()))
    kc = cfg.auth.providers["keycloak"]
    assert kc.groups_of({"groups": ["/db-admins"]}) == ["db-admins"]


def test_a_single_group_string_is_accepted(tmp_path):
    cfg = config_mod.load(write(tmp_path, both()))
    assert cfg.auth.providers["keycloak"].groups_of({"groups": "solo"}) == ["solo"]


def test_a_missing_groups_claim_is_no_groups_not_a_crash(tmp_path):
    cfg = config_mod.load(write(tmp_path, both()))
    assert cfg.auth.providers["keycloak"].groups_of({}) == []


def test_the_username_falls_back_through_the_claims_each_provider_uses(tmp_path):
    cfg = config_mod.load(write(tmp_path, both()))
    p = cfg.auth.providers["keycloak"]
    assert p.username_of({"preferred_username": "wimpie", "sub": "uuid"}) == "wimpie"
    assert p.username_of({"email": "a@b.c", "sub": "uuid"}) == "a@b.c"   # WeldForge
    assert p.username_of({"sub": "uuid"}) == "uuid"


def test_the_username_claim_can_be_pinned(tmp_path):
    auth = {"providers": [{"id": "k", "issuer": KEYCLOAK, "client_id": "x",
                           "username_claim": "email"}]}
    cfg = config_mod.load(write(tmp_path, auth))
    assert cfg.auth.providers["k"].username_of(
        {"preferred_username": "ignored", "email": "a@b.c"}) == "a@b.c"


# ── issuer selection ───────────────────────────────────────────────────────

def test_the_issuer_is_read_without_verifying_the_signature(tmp_path):
    """Only to pick which provider verifies it. A forged issuer then fails.

    The signature here is meaningless but must still be base64url — a real token
    always is, and PyJWT will not parse one that is not even when signature
    verification is off. A first draft of this test used the literal string
    "notasignature" and failed for that reason rather than for a real one.
    """
    import base64
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode()
    token = f'{seg({"alg": "RS256"})}.{seg({"iss": WELDFORGE, "sub": "x"})}.{signature}'
    assert providers_mod.issuer_of(token) == WELDFORGE


def test_rubbish_yields_no_issuer_rather_than_raising(tmp_path):
    assert providers_mod.issuer_of("not-a-token") is None
