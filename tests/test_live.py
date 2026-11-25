"""The real database, end to end: `brain init` in an empty folder, then the rules the FakeStore mirrors.

Skipped unless BRAIN_LIVE_TEST=1, because it starts Docker containers (and removes them, with their
volume, when it ends). Run it after any change to brain/schema.sql or brain/stack.py:

    BRAIN_LIVE_TEST=1 python3 -m pytest tests/test_live.py -q
"""
import os
import subprocess

import pytest

from brain import api, cli
from brain.config import Config
from brain.search import ask
from brain.store import BrainRefused, RestStore, VersionConflict

pytestmark = pytest.mark.skipif(os.getenv("BRAIN_LIVE_TEST") != "1", reason="set BRAIN_LIVE_TEST=1 (starts Docker)")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("livebrain")
    (root / "README.md").write_text("# live\n")
    try:
        assert cli.main(["init"], root=root) == 0
        yield RestStore(Config.load(root), timeout=10)
    finally:
        subprocess.run(["docker", "compose", "-f", str(root / ".brain" / "docker-compose.yml"), "down", "-v"],
                       capture_output=True, timeout=120)


def _draft(statement, author="lead", kind="rule", entity=("tool", "git")):
    return {"kind": kind, "statement": statement, "author": author, "section": "general",
            "evidence": [{"type": "path", "ref": "README.md"}], "entities": [{"type": entity[0], "name": entity[1]}]}


def test_the_database_enforces_what_the_fake_mirrors(store):
    repo = store.config.root
    old = api.write_fact(store, _draft("Use `git stash` to park the fix."), repo=repo)["fact"]
    assert old["status"] == "unverified" and old["version"] == 1 and old["verify_by"]
    assert store.resolve_fact_id(old["id"][:8]) == old["id"]
    with pytest.raises(BrainRefused):                                       # the author cannot verify
        api.verify_fact(store, old["id"], "verified", role="lead", output="ran it")
    with pytest.raises(BrainRefused, match="immutable"):
        store.patch_fact(old["id"], 1, {"statement": "A different statement."})
    with pytest.raises(VersionConflict):
        store.patch_fact(old["id"], 7, {"verify_note": "x"})
    assert api.verify_fact(store, old["id"], "verified", role="reviewer", output="ran it")["status"] == "verified"

    r = api.write_fact(store, _draft("Never `git stash` in the shared checkout.", author="builder"), repo=repo)
    assert r["status"] == "needs_judgement" and [c["id"] for c in r["candidates"]] == [old["id"]]
    new = api.write_fact(store, _draft("Never `git stash` in the shared checkout.", author="builder"), repo=repo,
                         judgements={old["id"]: "supersedes"})["fact"]
    closed = store.get("brain_facts", old["id"])
    assert closed["status"] == "superseded" and closed["valid_to"] is not None
    with pytest.raises(BrainRefused, match="write-once"):
        store.patch_fact(old["id"], closed["version"], {"valid_to": None})
    again = api.write_fact(store, _draft("Never `git stash` in the shared checkout.", author="builder"), repo=repo,
                           write_id=new["id"])
    assert again["fact"]["id"] == new["id"]                                 # a replayed write is the same fact

    pending = api.write_fact(store, _draft("Always `git stash` before a pull.", author="lead"), repo=repo,
                             interactive=False)["fact"]
    assert pending["judge_pending"] is True
    with pytest.raises(BrainRefused, match="judge"):
        api.verify_fact(store, pending["id"], "verified", role="reviewer", output="x")
    assert store.judge_pair(pending, new, "contradicts", "reviewer") == {"verdict": "contradicts", "replayed": False}
    assert store.get("brain_facts", pending["id"])["status"] == "disputed"
    assert store.get("brain_facts", pending["id"])["judge_pending"] is False
    with pytest.raises(BrainRefused, match="judgement_conflict"):
        store.judge_pair(store.get("brain_facts", pending["id"]), store.get("brain_facts", new["id"]), "unrelated", "reviewer")

    secs = [s["id"] for s in store.sections()]
    assert store.queue_count(secs, "reviewer") == 0 and store.queue_count(secs) == 0
    assert {f["id"] for f in store.facts_in_sections(secs)} == {new["id"], pending["id"]}
    assert [f["id"] for f in store.find_facts(new["entity_ids"], live_only=False)][0] == old["id"]   # oldest first


def test_search_episodes_and_the_append_only_log(store):
    repo = store.config.root
    api.write_fact(store, _draft("The parser accepts a byte order mark at the start.", kind="finding",
                                 entity=("component", "parser")), repo=repo, interactive=False)
    assert ask(store, "byte order mark", embed=False)[0]["statement"].startswith("The parser accepts")
    assert ask(store, "what does the parser do with a byte order mark in a file", embed=False)   # any-two-words fallback
    assert store.search_facts("zebra umbrella", any_word=True) == []
    assert store.match_facts(None) == [] and store.facts_missing_embedding(5)
    eps = api.add_episode(store, "handoff_line", "x" * 4000, author="lead")
    assert [e["part_no"] for e in eps] == [1, 2, 3] and eps[1]["parent_id"] == eps[0]["id"]
    with pytest.raises(BrainRefused):
        store._call("PATCH", f"brain_episodes?id=eq.{eps[0]['id']}", {"body": "rewritten"})
    with pytest.raises(BrainRefused):
        store._call("DELETE", f"brain_facts?kind=eq.finding")
    with pytest.raises(BrainRefused):
        store.insert("brain_entities", {"type": "tool", "name": "GIT", "section_id": store.sections()[0]["id"]})
    anon = RestStore(request=__import__("brain.store", fromlist=["http_request"]).http_request(store.config.url, ""))
    with pytest.raises(BrainRefused):                                       # no key: refused, never an empty brain
        anon.sections()
