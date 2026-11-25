"""The gate's pure checks: draft shape, the secret scan, and episode redaction."""
from brain.gate import (contains_secret, redact, secret_values_from_env, validate_draft,
                        validate_episode_body)
from conftest import make_config

CFG = make_config()

BASE = {"kind": "rule", "statement": "Never run pkill -f on this host.", "author": "lead",
        "entities": [{"type": "process", "name": "runner"}],
        "evidence": [{"type": "doc", "ref": "pyproject.toml"}]}


def _errs(**kw):
    return validate_draft(dict(BASE, **kw), secret_values=set(), authors=CFG.authors,
                          entity_types=CFG.entity_types)


def test_valid_draft_passes():
    assert _errs() == []


def test_evidence_rules():
    assert any("evidence" in e for e in _errs(evidence=[]))
    assert _errs(kind="observation", evidence=[], episode_ids=["e1"]) == []
    assert any("episode" in e for e in _errs(kind="observation", evidence=[], episode_ids=[]))
    assert any("evidence" in e for e in _errs(evidence=[{"type": "rumour", "ref": "x"}]))
    assert any("evidence" in e for e in _errs(evidence=[{"type": "path", "ref": ""}]))


def test_size_kind_entities_author():
    assert _errs(statement="x" * 601)
    assert _errs(statement="short")
    assert _errs(kind="config")                                       # no config kind
    assert _errs(entities=[])
    assert any("type" in e for e in _errs(entities=[{"type": "knobs", "name": "APP_X"}]))
    assert _errs(entities=[{"type": "knob", "name": "  "}])
    assert _errs(author="planner") == [] and _errs(author="operator") == []
    assert any("author" in e for e in _errs(author="unknown-role"))
    assert _errs(author="")


def test_secret_value_is_refused_and_never_echoed():
    secret = "sk_live_abcdef1234567890"
    errs = validate_draft(dict(BASE, detail=f"the key is {secret}"), secret_values={secret}, authors=CFG.authors,
                          entity_types=CFG.entity_types)
    assert errs and secret not in " ".join(errs)


def test_keyed_url_refused_but_public_url_and_key_id_allowed():
    assert _errs(evidence=[{"type": "doc", "ref": "https://rpc.example.com/v2/AbCdEf12GhIj34KlMn56"}])
    assert _errs(statement="Reads use https://api.example.com/?api-key=0f3c9a1e-5b7d-4c2a-9e8f-1a2b3c4d5e6f.")
    assert _errs(statement="The watcher reads region 7 from https://public.example.org first.") == []
    assert _errs(statement="Research reads use the vendor key e1f95763 only.") == []


def test_credential_shapes():
    assert contains_secret("token=abc123def456ghi789jkl0", set())
    assert contains_secret("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc", set())
    assert contains_secret("[" + ",".join(["12"] * 64) + "]", set())     # a raw keypair file's bytes
    assert not contains_secret("token: app_positions.opened_recorded_at", set())
    assert not contains_secret("tx 0x" + "ab" * 32 + " landed", set())    # a tx hash is not a key


def test_secret_values_from_env(tmp_path):
    p = tmp_path / ".env"
    p.write_text("\n".join([
        "BRAIN_SERVICE_KEY=abcdefghijklmnop123",
        "APP_LIVE_TP_PCT=25",
        "APP_RPC_POOL_URLS_7=https://public.example.org,https://rpc.example.com/v2/AbCdEf12GhIj34KlMn56",
        "APP_READ_URL_7=https://public.example.org",
        "APP_ROUTE_BYPASS=yes-this-is-long-enough",
        "# BRAIN_SERVICE_KEY=commentedout123456",
    ]))
    vals = secret_values_from_env(p)
    assert "abcdefghijklmnop123" in vals and "AbCdEf12GhIj34KlMn56" in vals
    assert "25" not in vals and "yes-this-is-long-enough" not in vals   # BYPASS is not PASS
    assert not any("example.org" in v for v in vals)                   # a public endpoint is not a secret
    assert "commentedout123456" not in vals


def test_episode_bodies_are_scanned_and_redact_keeps_the_rest():
    assert validate_episode_body("token=abc123def456ghi789jkl0", secret_values=set())
    assert validate_episode_body("plain handoff text", secret_values=set()) == []
    body = "set APP_X; key AbCdEf12GhIj34KlMn56 and https://rpc.example.com/v2/Zz99Yy88Xx77Ww66Vv55 done"
    text, n = redact(body, {"AbCdEf12GhIj34KlMn56"})
    assert n == 2 and "AbCdEf12" not in text and "Zz99Yy88" not in text and "[REDACTED]" in text
    assert "APP_X" in text and validate_episode_body(text, secret_values={"AbCdEf12GhIj34KlMn56"}) == []
