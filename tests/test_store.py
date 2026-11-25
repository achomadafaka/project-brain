"""The store: RestStore's request shapes and error mapping, and FakeStore's copy of the database rules."""
import pytest

from fake_store import FakeStore
from brain import group_of
from brain.config import Config
from brain.store import BrainMisconfigured, BrainRefused, BrainUnavailable, RestStore, VersionConflict, pg_array

EV = [{"type": "doc", "ref": "x", "check": "unchecked"}]


class Recorder:
    """A fake transport: records (method, path, body) and returns or raises what the test queued."""

    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, body))
        a = self.answers.pop(0) if self.answers else []
        if isinstance(a, BaseException):
            raise a
        return a


def _store():
    s = FakeStore()
    sec = s.insert("brain_sections", {"slug": "infra/deploy", "title": "Deploy"})
    return s, sec


def _ent(s, sec, name="git", type="tool", **kw):
    return s.insert("brain_entities", dict({"type": type, "name": name, "section_id": sec["id"]}, **kw))


def _fact(s, e, statement, author="lead", kind="rule", **kw):
    return s.insert("brain_facts", dict({"kind": kind, "statement": statement, "entity_ids": [e["id"]],
                                         "evidence": EV, "author": author}, **kw))


def test_groups():
    assert group_of("trap") == group_of("settled") == "normative"
    assert group_of("finding") == "descriptive"


# ------------------------------------------------------------------ RestStore
def test_rest_store_refuses_to_start_without_the_service_key(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_SERVICE_KEY", raising=False)
    monkeypatch.delenv("BRAIN_URL", raising=False)
    cfg = Config(tmp_path, {"rest_port": 54000})
    with pytest.raises(BrainMisconfigured):            # no .brain/brain.env yet
        RestStore(cfg)
    cfg.dir.mkdir()
    cfg.env_file.write_text("BRAIN_SERVICE_KEY=service-key-value\n")
    RestStore(cfg)
    with pytest.raises(BrainMisconfigured):            # a key but no port: the stack was never set up
        RestStore(Config(tmp_path, {}))


def test_http_errors_keep_their_status_and_an_outage_is_unavailable():
    from brain.store import http_request
    s = RestStore(request=http_request("http://127.0.0.1:9", "k"), timeout=1)   # nothing listens on port 9
    with pytest.raises(BrainUnavailable):
        s.get("brain_facts", "x")


@pytest.mark.parametrize("err, expected", [
    (RuntimeError("HTTP 503 GET x: upstream down"), BrainUnavailable),
    (RuntimeError("HTTP 500 GET x: boom"), BrainUnavailable),
    (RuntimeError("HTTP 429 GET x: slow down"), BrainUnavailable),
    (RuntimeError("HTTP 408 GET x: timeout"), BrainUnavailable),
    (RuntimeError("HTTP 402 GET x: billing restriction"), BrainUnavailable),
    (RuntimeError("HTTP 400 POST rpc/brain_write_fact: version_conflict"), VersionConflict),
    (RuntimeError("HTTP 400 POST brain_facts: new row violates check constraint"), BrainRefused),
    (RuntimeError("HTTP 409 POST brain_entities: duplicate key"), BrainRefused),
    (RuntimeError("connection refused"), BrainUnavailable),
    (OSError("network unreachable"), BrainUnavailable),
])
def test_error_mapping(err, expected):
    with pytest.raises(expected):
        RestStore(request=Recorder(err)).get("brain_facts", "x")


def test_find_entity_is_exact_never_a_pattern():
    rec = Recorder([], [])
    RestStore(request=rec).find_entity("knob", "APP_A_B")
    (m1, p1, _), (m2, p2, _) = rec.calls
    assert "name_lc=eq.app_a_b" in p1 and "ilike" not in p1 and "like" not in p1
    assert "aliases=cs." in p2 and "%22app_a_b%22" in p2         # quoted, lower-cased element


def test_array_literal_quotes_special_characters():
    assert pg_array(['a,b', 'q"x', "{c}"]) == '{"a,b","q\\"x","{c}"}'


def test_section_and_entity_reads_are_rpcs_not_urls():
    rec = Recorder([], [], [], 7)
    s = RestStore(request=rec)
    s.facts_in_sections(["s1", "s2"], ("rule",))
    s.find_facts(["e1"] * 3000)
    s.unverified_queue("builder", ["s1"], 10)
    assert s.queue_count(["s1"], "builder") == 7
    paths = [p for _, p, _ in rec.calls]
    from brain.store import FACT_COLUMNS
    assert paths == ["rpc/brain_facts_in_sections?select=" + FACT_COLUMNS,
                     "rpc/brain_facts_for_entities?select=" + FACT_COLUMNS + "&order=id.asc&limit=500",
                     "rpc/brain_facts_in_sections?select=" + FACT_COLUMNS, "rpc/brain_queue_count"]
    assert "embedding" not in FACT_COLUMNS and "search_vector" not in FACT_COLUMNS
    assert rec.calls[1][2]["p_entity_ids"] == ["e1"] * 3000             # in the body, not the URL
    q = rec.calls[2][2]
    assert q["p_queue_only"] and q["p_oldest_first"] and q["p_exclude_author"] == "builder"


def test_vector_hydration_filters_facts_withdrawn_after_match():
    def request(method, path, body, headers, timeout):
        if path == "rpc/brain_match_facts":
            return [{"id": "f1", "similarity": 1.0}]
        assert method == "GET"
        if "status=in." in path and "valid_to=is.null" in path:
            return []
        return [{"id": "f1", "status": "superseded", "valid_to": "2026-09-29T00:00:00Z"}]
    assert RestStore(request=request).match_facts([0.0] * 384) == []


def test_patch_tells_a_missing_row_from_a_conflict():
    with pytest.raises(VersionConflict):
        RestStore(request=Recorder([], [{"id": "f"}])).patch_fact("f", 1, {"verify_note": "x"})
    with pytest.raises(BrainRefused):
        RestStore(request=Recorder([], [])).patch_fact("f", 1, {"verify_note": "x"})


@pytest.mark.parametrize("read", [
    lambda s: s.get("brain_facts", "f"),
    lambda s: s.find_facts(["e"], live_only=False),
    lambda s: s.search_facts("APP_A_B", limit=7),
    lambda s: s.match_facts([0.0] * 384, limit=7),
])
def test_normal_fact_reads_return_full_provenance_without_vectors(read):
    from urllib.parse import parse_qs
    from brain.store import FACT_COLUMNS

    columns = set(FACT_COLUMNS.split(','))
    fact = dict.fromkeys(columns)
    fact.update(id="f", statement="A complete historical fact.", status="retracted",
                valid_to="2026-10-01T00:00:00Z", version=9,
                evidence=EV, detail="Keep full detail.", author_session="s1",
                embedding=[0.1] * 384, search_vector="large full-text representation")
    calls = []

    def request(method, path, body, headers, timeout):
        calls.append((method, path, body))
        if path == "rpc/brain_match_facts":
            return [{"id": "f", "similarity": 1.0}]
        query = parse_qs(path.split('?', 1)[1])
        assert 'embedding' not in query and 'search_vector' not in query
        if 'id' in query and query['id'][0].startswith('gt.'):
            return []
        selected = query.get('select', ['*'])[0].split(',')
        return [{k: v for k, v in fact.items() if '*' in selected or k in selected}]

    result = read(RestStore(request=request))
    rows = list(result.values()) if isinstance(result, dict) and 'id' not in result else (
        [result] if isinstance(result, dict) else result)
    assert rows == [{k: fact[k] for k in columns}]
    for _, path, _ in calls:
        if path != "rpc/brain_match_facts":
            query = parse_qs(path.split('?', 1)[1])
            assert set(query['select'][0].split(',')) == columns


@pytest.mark.parametrize("table,fields,read", [
    ("brain_entities", "id,type,name,name_lc,aliases,summary,section_id,created_at,updated_at",
     lambda s: s.get("brain_entities", "e")),
    ("brain_entities", "id,type,name,name_lc,aliases,summary,section_id,created_at,updated_at",
     lambda s: s.find_entity("knob", "APP_A_B")),
    ("brain_episodes", "id,kind,body,parent_id,part_no,author,session_id,occurred_at,recorded_at,source_ref",
     lambda s: s.get("brain_episodes", "p")),
    ("brain_episodes", "id,kind,body,parent_id,part_no,author,session_id,occurred_at,recorded_at,source_ref",
     lambda s: s.episodes_since(("handover",), "2026-01-01T00:00:00Z", 7)),
])
def test_normal_entity_and_episode_reads_keep_functional_fields(table, fields, read):
    from urllib.parse import parse_qs
    rec = Recorder([{"id": "row", "body": "full immutable body", "aliases": ["alias"]}])
    read(RestStore(request=rec))
    path = rec.calls[0][1]
    assert path.startswith(table + '?')
    query = parse_qs(path.split('?', 1)[1])
    assert set(query.get('select', ['*'])[0].split(',')) == set(fields.split(','))


def test_default_projection_preserves_entity_name_and_alias_filters():
    from urllib.parse import parse_qs
    rec = Recorder([], [])
    RestStore(request=rec).find_entity("knob", "APP_A_B")
    queries = [parse_qs(path.split('?', 1)[1]) for _, path, _ in rec.calls]
    assert all('embedding' not in q.get('select', ['*'])[0] for q in queries)
    assert queries[0]['name_lc'] == ['eq.app_a_b']
    assert queries[1]['aliases'] == ['cs.{"app_a_b"}']


def test_an_outage_queues_and_keeps_write_identity_until_recovery(tmp_path):
    import json
    from pathlib import Path
    from brain import api

    def restricted(method, path, body, headers, timeout):
        raise RuntimeError(f"HTTP 402 {method} {path}: billing restriction")

    from conftest import make_config
    offline = RestStore(make_config(), request=restricted)
    repo = Path(__file__).resolve().parents[1]
    draft = dict(kind='rule', statement='Keep graph writes queued while the database is unavailable.',
                 author='lead', section='general', entities=[dict(type='tool', name='git')],
                 evidence=[dict(type='path', ref='brain/store.py')])
    outbox = tmp_path / 'outbox.jsonl'
    assert api.write_fact(offline, draft, repo=repo, outbox=outbox)['status'] == 'queued'
    identity = json.loads(outbox.read_text())['write_id']
    assert api.flush_outbox(offline, outbox, repo=repo, secret_values=set()) == {
        'written': 0, 'kept': 1, 'refused': 0}
    assert json.loads(outbox.read_text())['write_id'] == identity
    online = FakeStore()
    online.insert('brain_sections', dict(slug='general', title='General'))
    assert api.flush_outbox(online, outbox, repo=repo, secret_values=set()) == {
        'written': 1, 'kept': 0, 'refused': 0}
    assert [f['id'] for f in online.rows['brain_facts']] == [identity]
    assert api.flush_outbox(online, outbox, repo=repo, secret_values=set())['written'] == 0


# ------------------------------------------------------------------ FakeStore (the database's rules)
def test_patch_checks_version():
    s, sec = _store()
    f = _fact(s, _ent(s, sec, "APP_X", "knob"), "Never raise APP_X above 5.")
    s.patch_fact(f["id"], 1, {"status": "disputed"})
    with pytest.raises(VersionConflict):
        s.patch_fact(f["id"], 1, {"status": "verified", "verified_by": "builder"})


def test_self_verification_and_verify_rules_are_refused_like_the_database():
    s, sec = _store()
    e = _ent(s, sec)
    f = _fact(s, e, "Some rule text here.")
    with pytest.raises(BrainRefused):
        s.patch_fact(f["id"], 1, {"status": "verified", "verified_by": "lead"})      # author
    with pytest.raises(BrainRefused):
        s.patch_fact(f["id"], 1, {"status": "verified"})                             # no verifier
    s.patch_fact(f["id"], 1, {"status": "disputed"})
    with pytest.raises(BrainRefused, match="dispute"):
        s.patch_fact(f["id"], 2, {"status": "verified", "verified_by": "builder"})
    g = _fact(s, e, "Another rule text.", judge_pending=True)
    with pytest.raises(BrainRefused, match="judge"):
        s.patch_fact(g["id"], 1, {"status": "verified", "verified_by": "builder"})


def test_history_is_immutable_and_closed_facts_stay_closed():
    s, sec = _store()
    e = _ent(s, sec)
    f = _fact(s, e, "Some rule text here.")
    for patch in ({"statement": "changed text here"}, {"valid_from": "2020-01-01T00:00:00+00:00"},
                  {"entity_ids": ["other"]}):
        with pytest.raises(BrainRefused, match="immutable"):
            s.patch_fact(f["id"], 1, patch)
    s.patch_fact(f["id"], 1, {"status": "retracted", "valid_to": "2026-09-28T13:00:00+00:00"})
    with pytest.raises(BrainRefused, match="write-once"):
        s.patch_fact(f["id"], 2, {"valid_to": None})


def test_new_evidence_voids_a_verification_but_a_check_annotation_does_not():
    s, sec = _store()
    e = _ent(s, sec)
    f = _fact(s, e, "Some rule text here.")
    v = s.patch_fact(f["id"], 1, {"status": "verified", "verified_by": "builder"})
    v = s.patch_fact(f["id"], v["version"], {"evidence": [dict(EV[0], check="ok")]})
    assert v["status"] == "verified" and v["verified_by"] == "builder"
    v = s.patch_fact(f["id"], v["version"], {"evidence": [{"type": "path", "ref": "pyproject.toml"}]})
    assert v["status"] == "unverified" and v["verified_by"] is None
    r = s.patch_fact(f["id"], v["version"], {"status": "refuted", "verified_by": "reviewer"})
    r = s.patch_fact(f["id"], r["version"], {"evidence": [{"type": "path", "ref": "AGENTS.md"}]})
    assert r["status"] == "unverified"                                  # new evidence reopens a refutation


def test_evidence_broken_stays_readable():
    s, sec = _store()
    e = _ent(s, sec)
    f = _fact(s, e, "Some rule text here.")
    s.patch_fact(f["id"], 1, {"status": "evidence_broken"})
    assert [x["id"] for x in s.find_facts([e["id"]])] == [f["id"]]


def test_entities_need_a_section_a_known_type_and_unique_aliases():
    s, sec = _store()
    with pytest.raises(BrainRefused, match="section"):
        s.insert("brain_entities", {"type": "knob", "name": "APP_X", "section_id": None})
    with pytest.raises(BrainRefused, match="type"):
        s.insert("brain_entities", {"type": "knobs", "name": "APP_X", "section_id": sec["id"]})
    svc = _ent(s, sec, "billing", "service", aliases=["Pay", "PAYMENTS", "billing"])
    assert svc["aliases"] == ["pay", "payments"]
    assert s.find_entity("service", "PayMents")["id"] == svc["id"]
    with pytest.raises(BrainRefused, match="alias_conflict"):
        _ent(s, sec, "pay", "service")


def test_knobs_that_differ_at_an_underscore_are_different_entities():
    s, sec = _store()
    a = _ent(s, sec, "APP_A_B", "knob")
    b = _ent(s, sec, "APPXA_B", "knob")
    assert s.find_entity("knob", "APP_A_B")["id"] == a["id"]
    assert s.find_entity("knob", "appxa_b")["id"] == b["id"]


def test_queue_is_the_true_oldest_and_counts_everything():
    s, sec = _store()
    e = _ent(s, sec)
    ids = [_fact(s, e, f"Rule number {i} for the queue.", author="lead")["id"] for i in range(8)]
    assert [f["id"] for f in s.unverified_queue("builder", [sec["id"]], 3)] == ids[:3]
    assert s.unverified_queue("lead", [sec["id"]], 3) == []                 # own facts are not queued
    assert s.queue_count([sec["id"]], "builder") == 8


@pytest.mark.parametrize('server_cap', [137, 1000])
def test_entity_fact_lookup_pages_past_server_cap_and_preserves_oldest_order(server_cap):
    from urllib.parse import parse_qs, urlsplit
    facts = [{'id': f'{i:08d}', 'recorded_at': f'{1300-i:08d}'} for i in range(1251)]
    calls = []
    def request(method, path, body, headers, timeout):
        calls.append((method, path, body))
        query = parse_qs(urlsplit(path).query)
        after = query.get('id', ['gt.'])[0][3:]
        limit = min(int(query.get('limit', [server_cap])[0]), server_cap)
        return [f for f in facts if f['id'] > after][:limit]
    result = RestStore(request=request).find_facts(['entity'] * 3000, ['rule'], False)
    assert result == sorted(facts, key=lambda f: (f['recorded_at'], f['id']))
    assert len(calls) > 1
    assert all(m == 'POST' and b == {'p_entity_ids': ['entity'] * 3000,
        'p_kinds': ['rule'], 'p_live_only': False} for m, _, b in calls)
    assert all('entity_ids' not in parse_qs(urlsplit(path).query) for _, path, _ in calls)


def test_entity_fact_lookup_refuses_nonadvancing_page():
    with pytest.raises(BrainRefused, match='paging did not advance'):
        RestStore(request=Recorder([{'id': 'a'}], [{'id': 'a'}])).find_facts(['entity'])
