"""The `brain` CLI against the FakeStore, and hybrid search."""
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brain import api, cli
from brain.search import ask
from brain.store import BrainUnavailable, RestStore
from conftest import make_config
from fake_store import FakeStore

ROOT = Path(__file__).resolve().parents[1]
DOC = [{"type": "path", "ref": "pyproject.toml"}]


@pytest.fixture()
def project(tmp_path):
    """A project folder with a config and one file to cite. Not a git repository."""
    (tmp_path / ".brain").mkdir()
    (tmp_path / ".brain" / "config.json").write_text(json.dumps(make_config().data))
    (tmp_path / "README.md").write_text("# demo\n")
    return tmp_path


def _store(root=ROOT):
    s = FakeStore(make_config(root))
    s.ensure_sections(["general"])
    return s


def _write(s, statement, kind="rule", entity=("tool", "git"), evidence=DOC, author="lead"):
    return api.write_fact(s, {"kind": kind, "statement": statement, "author": author, "section": "general",
                              "evidence": list(evidence), "entities": [{"type": entity[0], "name": entity[1]}]},
                          repo=ROOT, interactive=False)["fact"]


def test_help_lists_every_command():
    out = subprocess.run([sys.executable, "-m", "brain", "--help"], capture_output=True, text=True, timeout=60,
                         cwd=ROOT).stdout
    for cmd in ("init", "boot", "ask", "entity", "write", "verify", "retract", "judge", "queue", "episode",
                "handover", "flush", "install-hook"):
        assert cmd in out


def test_write_args_parse_with_section_and_judgements():
    cfg = make_config()
    argv = ["write", "--role", "lead", "--kind", "rule", "--entity", "tool:git", "--evidence", "path:README.md",
            "--evidence", "doc:https://x/y", "--judge", "abc=duplicate", "Never `git stash`."]
    d = cli.draft_from_args(cli.build_parser().parse_args(argv), cfg)
    assert d["entities"] == [{"type": "tool", "name": "git"}] and d["section"] == "general"   # the default section
    assert d["evidence"] == [{"type": "path", "ref": "README.md"}, {"type": "doc", "ref": "https://x/y"}]
    assert d["judgements"] == {"abc": "duplicate"} and d["operator_order"] is False
    d = cli.draft_from_args(cli.build_parser().parse_args(argv[:1] + ["--section", "infra/deploy"] + argv[1:]), cfg)
    assert d["section"] == "infra/deploy"


def test_exit_codes_through_main(project, capsys):
    s = _store(project)
    ok = ["write", "--role", "lead", "--kind", "rule", "--entity", "tool:git", "--evidence", "path:README.md"]
    assert cli.main(ok + ["Never `git stash` in the shared checkout."], store=s) == 0
    assert cli.main(ok + ["Do not `git stash`; park it on a branch."], store=s) == 3
    assert cli.main(ok[:-2] + ["--evidence", "path:no/such.py", "A rule about a ghost file."], store=s) == 2
    assert cli.main(["write", "--role", "unknown-role", "--kind", "rule", "--entity", "tool:git",
                     "--evidence", "path:README.md", "Unknown role tries to write."], store=s) == 2
    fid = s.rows["brain_facts"][0]["id"]
    capsys.readouterr()
    assert cli.main(["verify", fid[:8], "verified", "--role", "builder"], store=s) == 0
    assert "verified_partial" in capsys.readouterr().out                       # no output file: partial
    assert cli.main(["verify", "no-such-id", "verified", "--role", "builder"], store=s) == 2
    assert cli.main(["verify", fid, "verified", "--role", "lead"], store=s) == 2     # the author: refused
    assert "REFUSED" in capsys.readouterr().out
    assert cli.main(["entity", "tool", "git"], store=s) == 0
    assert "path:README.md [pending_push]" in capsys.readouterr().out          # evidence checks are shown
    assert cli.main(["entity", "tool", "nothing"], store=s) == 2


def test_queue_episode_handover_and_retract_through_main(project, capsys):
    s = _store(project)
    s.clock = datetime.now(timezone.utc)
    w = ["write", "--role", "lead", "--kind", "task", "--entity", "actor:lead", "--evidence", "path:README.md"]
    assert cli.main(w + ["Add the regression test for the parser."], store=s) == 0
    fid = s.rows["brain_facts"][0]["id"]
    capsys.readouterr()
    assert cli.main(["queue", "--role", "reviewer"], store=s) == 0
    assert fid[:8] in capsys.readouterr().out
    assert cli.main(["queue", "--role", "lead"], store=s) == 0                 # own facts are not queued
    assert capsys.readouterr().out == ""
    assert cli.main(["episode", "--role", "lead", "--kind", "handoff_line", "parser test started"], store=s) == 0
    assert cli.main(["episode", "--role", "nobody", "--kind", "note", "x"], store=s) == 2
    assert cli.main(["boot", "lead"], store=s) == 0
    out = capsys.readouterr().out
    assert "parser test started" in out and "Add the regression test" in out
    assert cli.main(["handover", "lead"], store=s) == 0
    assert "brain boot lead" in capsys.readouterr().out
    assert cli.main(["retract", fid[:8], "--role", "lead", "--reason-kind", "done", "--reason", "shipped"], store=s) == 0
    assert s.get("brain_facts", fid)["status"] == "retracted"
    assert cli.main(["boot", "nobody"], store=s) == 2


def test_an_outage_queues_the_write_and_flush_replays_it(project, capsys):
    s = _store(project)

    class Down(FakeStore):
        def get(self, table, id):
            raise BrainUnavailable("down")
    down = Down(s.config)
    w = ["write", "--role", "lead", "--kind", "rule", "--entity", "tool:git", "--evidence", "path:README.md",
         "Commit with explicit pathspecs only."]
    assert cli.main(w, store=down) == 4
    assert (project / ".brain" / "outbox.jsonl").read_text().strip()
    assert cli.main(["flush"], store=s) == 0
    assert [f["statement"] for f in s.rows["brain_facts"]] == ["Commit with explicit pathspecs only."]
    assert "'written': 1" in capsys.readouterr().out


def test_no_brain_here_is_a_plain_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    assert cli.main(["ask", "anything"], root=tmp_path) == 2
    assert "brain init" in capsys.readouterr().out


def test_init_writes_the_files_keeps_secrets_private_and_is_idempotent(tmp_path, capsys):
    assert cli.main(["init", "--no-start", "--roles", "Ada, bob"], root=tmp_path) == 0
    out = capsys.readouterr().out
    d = tmp_path / ".brain"
    cfg = json.loads((d / "config.json").read_text())
    assert list(cfg["roles"]) == ["ada", "bob"] and isinstance(cfg["rest_port"], int)
    env = (d / "brain.env").read_text()
    assert stat.S_IMODE((d / "brain.env").stat().st_mode) == 0o600
    values = [line.split("=", 1)[1] for line in env.splitlines()]
    assert len(values) == 5 and not any(v in out for v in values)             # no secret is printed
    assert "brain.env" in (d / ".gitignore").read_text()
    compose = (d / "docker-compose.yml").read_text()
    assert f'"127.0.0.1:{cfg["rest_port"]}:3000"' in compose and not any(v in compose for v in values)
    assert cli.main(["init", "--no-start"], root=tmp_path) == 0               # again: nothing is rewritten
    assert (d / "brain.env").read_text() == env and json.loads((d / "config.json").read_text()) == cfg
    from brain.config import Config
    from brain.gate import secret_values_from_env
    assert Config.load(tmp_path).service_key() in secret_values_from_env(d / "brain.env")   # the gate knows them


def test_ask_fuses_and_expands():
    s = _store()
    for st in ("Never `git stash` in the shared checkout.", "Commit with explicit pathspecs only."):
        _write(s, st)
    got = ask(s, "stash", embed=False)
    assert got[0]["statement"].startswith("Never") and len(got) == 2


def test_embedding_threads_are_pinned_and_queries_never_download(monkeypatch):
    import brain.embed as emb
    seen = {}

    class FakeTE:
        def __init__(self, **kw):
            seen.update(kw)

        def embed(self, texts):
            return [[0.5, 1] for _ in texts]
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setitem(sys.modules, "fastembed", type(sys)("fastembed"))
    sys.modules["fastembed"].TextEmbedding = FakeTE
    monkeypatch.delenv("BRAIN_EMBED_THREADS", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    assert emb.embed_query("x") == [0.5, 1.0]
    assert seen["threads"] == 4 and os.environ["HF_HUB_OFFLINE"] == "1"       # not every core; no download
    monkeypatch.setattr(emb, "_model", None)


def test_vector_search_is_off_unless_the_project_opts_in(monkeypatch):
    calls = []
    monkeypatch.setattr("brain.search.embed_query", lambda q: calls.append(q) or [0.0])
    s = _store()
    _write(s, "Never `git stash` in the shared checkout.")
    ask(s, "stash")
    assert calls == []
    s.config.embeddings = True
    ask(s, "stash")
    assert calls == ["stash"]


def test_importing_the_cli_does_not_touch_the_environment():
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    code = ("import os\nbefore = dict(os.environ)\nimport brain.cli, brain.hook\n"
            "print(sorted(set(os.environ.items()) ^ set(before.items())))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stderr[-400:]
    assert r.stdout.strip() == "[]"


def test_the_boot_budget_holds_when_the_alarm_lands_inside_a_broad_except(project, monkeypatch, capsys):
    monkeypatch.setattr(cli, "BOOT_BUDGET_S", 1)

    class Slow(FakeStore):
        def sections(self, slugs=None):
            try:
                time.sleep(5)
            except Exception:                    # an Exception raised by the alarm would be swallowed here
                pass
            return []
    t = time.monotonic()
    assert cli.main(["boot", "lead"], store=Slow(make_config(project))) == 0
    assert time.monotonic() - t < 3
    assert "BRAIN UNREACHABLE" in capsys.readouterr().out


def test_the_cli_boot_rides_out_a_slow_spell(project, capsys):
    # Every call answers but takes 0.8 s. A role boot makes several calls; the hook's 2.5 s budget
    # would fall back to the snapshot, the CLI's does not.
    s = _store(project)
    s.clock = datetime.now(timezone.utc)          # the CLI boots at real now
    api.write_fact(s, {"kind": "task", "statement": "OPEN: read the slow-spell boot.", "author": "lead",
                       "section": "general", "evidence": [{"type": "path", "ref": "README.md"}],
                       "entities": [{"type": "actor", "name": "lead"}]}, repo=project, interactive=False)

    class Slow:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            attr = getattr(self._inner, name)

            def call(*a, **kw):
                time.sleep(0.8)
                return attr(*a, **kw)
            return call if callable(attr) else attr
    assert cli.main(["boot", "lead"], store=Slow(s)) == 0
    out = capsys.readouterr().out
    assert "BRAIN UNREACHABLE" not in out and "read the slow-spell boot" in out


def test_search_keeps_setting_path_and_slug_tokens():
    # Keeping only isalnum() words turned "APP_LIVE_CHASE_MAX_PCT" (an exact key, the commonest
    # query) into an empty query that returned nothing.
    s = _store()
    _write(s, "APP_LIVE_CHASE_MAX_PCT is shared with the batch job; never widen it.",
           entity=("knob", "APP_LIVE_CHASE_MAX_PCT"))
    assert [f["statement"][:22] for f in ask(s, "APP_LIVE_CHASE_MAX_PCT", embed=False)] == ["APP_LIVE_CHASE_MAX_PCT"]
    calls = []
    rest = RestStore(request=lambda method, path, body, headers, timeout: calls.append((path, body)) or [])
    rest.search_facts("APP_LIVE_CHASE_MAX_PCT scripts/tool.py", any_word=True)
    path, body = calls[0]
    assert path.startswith("rpc/brain_search_facts?select=") and "embedding" not in path
    assert body == {"p_query": "APP LIVE CHASE MAX PCT scripts tool py", "p_any": True, "p_limit": 20}


def test_ask_falls_back_to_any_two_words_when_no_fact_has_them_all():
    class Strict(FakeStore):
        seen = []

        def search_facts(self, q, limit=20, any_word=False):
            self.seen.append(any_word)
            return super().search_facts(q, limit) if any_word else []
    s = Strict(make_config(ROOT))
    s.ensure_sections(["general"])
    _write(s, "Never `git stash` in the shared checkout.")
    assert ask(s, "how do I stash my work", embed=False) and s.seen == [False, True]


def test_verify_refuses_env_file_and_injected_credentials(project, monkeypatch, capsys):
    (project / ".env").write_text("PUBLIC_FLAG=1\nVENDOR_API_KEY=UnmistakablyFakeFileCredential\n")
    (project / ".brain" / "brain.env").write_text("POSTGRES_PASSWORD=UnmistakablyFakeStackCredential\n")
    monkeypatch.setenv("INJECTED_API_KEY", "UnmistakablyFakeShellCredential")
    s = _store(project)
    fact = api.write_fact(s, {"kind": "rule", "statement": "Commit with explicit paths only.", "author": "lead",
                              "section": "general", "evidence": [{"type": "path", "ref": "README.md"}],
                              "entities": [{"type": "tool", "name": "git"}]}, repo=project)["fact"]
    for fake in ("UnmistakablyFakeFileCredential", "UnmistakablyFakeStackCredential", "UnmistakablyFakeShellCredential"):
        output = project / "output.txt"
        output.write_text("upstream rejected bearer " + fake)
        assert cli.main(["verify", fact["id"], "verified", "--role", "builder", "--output-file", str(output)],
                        store=s) == 2
        assert fake not in capsys.readouterr().out
        assert s.get("brain_facts", fact["id"])["verify_note"] is None
        w = ["write", "--role", "lead", "--kind", "finding", "--entity", "tool:git", "--evidence", "path:README.md"]
        assert cli.main(w + [f"The vendor accepts {fake} as a bearer."], store=s) == 2
        assert fake not in capsys.readouterr().out
    assert len(s.rows["brain_facts"]) == 1


def test_the_cli_snapshot_fallback_uses_the_boot_limit(project, monkeypatch, capsys):
    (project / ".brain" / "snapshot.json").write_text(
        json.dumps({"at": "2026-09-30T09:00:00Z", "role": "lead", "text": "X" * 20000}))

    def down(config):
        raise BrainUnavailable("down")
    monkeypatch.setattr(cli, "RestStore", down)
    assert cli.main(["boot", "lead"], root=project) == 0
    out = capsys.readouterr().out
    assert "BRAIN UNREACHABLE" in out and len(out.rstrip("\n")) <= cli.views.BOOT_LIMIT


class _HubStore(FakeStore):
    """The vector side answers every query with the same old, generic rows."""
    hubs = []

    def match_facts(self, embedding, limit=20):
        return [dict(f) for f in self.hubs] if embedding is not None else []


def _ranked_store(monkeypatch):
    monkeypatch.setattr("brain.search.embed_query", lambda q: [0.0])      # never load a real model in a test
    cfg = make_config(ROOT)
    cfg.embeddings = True
    s = _HubStore(cfg)
    s.ensure_sections(["general"])
    for i in range(5):
        _write(s, f"Generic hub rule number {i} about shells.", entity=("tool", f"hub{i}"))
    s.hubs = list(s.rows["brain_facts"])
    return s


def _fact(s, kind, text, recorded_at):
    _write(s, text, kind=kind, entity=("tool", text[:20]))
    s.rows["brain_facts"][-1]["recorded_at"] = s.rows["brain_facts"][-1]["valid_from"] = recorded_at


def test_ask_keyword_hits_outrank_vector_only_hub_rows(monkeypatch):
    s = _ranked_store(monkeypatch)
    for i in range(3):
        _fact(s, "finding", f"Octopus plan {i} was measured.", "2026-10-01T00:00:00+00:00")
    got = [f["statement"] for f in ask(s, "octopus", embed=True)]
    assert [g.startswith("Octopus") for g in got[:3]] == [True, True, True], got


def test_ask_recent_decision_beats_older_fact_one_keyword_rank_behind(monkeypatch):
    s = _ranked_store(monkeypatch)
    _fact(s, "finding", "Octopus eval-1 old finding from March.", "2026-03-01T00:00:00+00:00")
    _fact(s, "decision", "Octopus eval-1 operator decision today.", "2026-10-30T09:00:00+00:00")
    got = [f["statement"] for f in ask(s, "octopus", embed=True, now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))]
    assert got[0].startswith("Octopus eval-1 operator decision"), got


def test_ask_unknown_age_gets_no_recency_boost(monkeypatch):
    s = _ranked_store(monkeypatch)
    _fact(s, "finding", "Octopus first keyword hit.", None)
    _fact(s, "finding", "Octopus second keyword hit.", "not-a-time")
    got = [f["statement"] for f in ask(s, "octopus", embed=True, now=datetime(2026, 10, 30, tzinfo=timezone.utc))]
    assert got[:2] == ["Octopus first keyword hit.", "Octopus second keyword hit."]


def test_ask_recency_uses_event_date_not_the_day_it_was_recorded(monkeypatch):
    s = _ranked_store(monkeypatch)
    _fact(s, "finding", "Octopus eval-1 old finding recorded late.", "2026-10-29T00:00:00+00:00")
    s.rows["brain_facts"][-1]["valid_from"] = "2026-03-01T00:00:00+00:00"                 # March fact, recorded yesterday
    _fact(s, "decision", "Octopus eval-1 operator decision today.", "2026-10-30T09:00:00+00:00")
    got = [f["statement"] for f in ask(s, "octopus", embed=True, now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))]
    assert got[0].startswith("Octopus eval-1 operator decision"), got


def test_ask_caps_vector_only_hits_at_five_and_reports_the_cut(monkeypatch):
    s = _ranked_store(monkeypatch)
    for i in range(7):                                                    # 12 hub rows in all, none matching the keyword
        _write(s, f"Extra hub rule {i} about shells.", entity=("tool", f"xhub{i}"))
    s.hubs = [f for f in s.rows["brain_facts"] if "hub rule" in f["statement"].lower()]
    _fact(s, "finding", "Octopus keyword hit one.", "2026-10-01T00:00:00+00:00")
    stats = {}
    got = ask(s, "octopus", embed=True, stats=stats, limit=50)
    hubs = [f for f in got if "hub rule" in f["statement"].lower()]
    assert len(hubs) == 5 and got[0]["statement"].startswith("Octopus"), [g["statement"] for g in got]
    assert stats == {"vector_only_seen": len(s.hubs), "vector_only_cut": len(s.hubs) - 5, "vector_only_cap": 5}


def test_ask_cap_never_cuts_a_hit_that_also_matched_by_keyword_or_sha(monkeypatch):
    s = _ranked_store(monkeypatch)
    for i in range(8):
        _fact(s, "finding", f"Octopus shared hit {i}.", "2026-10-01T00:00:00+00:00")
    s.hubs = list(s.rows["brain_facts"])                                   # every fact is ALSO a vector hit
    stats = {}
    got = ask(s, "octopus", embed=True, stats=stats, limit=50)
    assert sum(g["statement"].startswith("Octopus shared") for g in got) == 8 and stats["vector_only_seen"] == len(s.hubs) - 8


FULL = "5fac49b8d" + "0123456789abcdef0123456789abcde"       # 40 hex


def _sha_store():
    s = _store()
    _write(s, "Merged the coverage sample with no keyword in common.", kind="finding", entity=("tool", "sampler"),
           evidence=[{"type": "query", "ref": "git log"}, {"type": "sha", "ref": FULL[:9]}])
    _write(s, "Entity linked change without sha evidence.", kind="decision", entity=("commit", "ab12cd34" + "e" * 32))
    return s


def test_ask_sha_is_a_retrieval_key_short_and_full(monkeypatch):
    # The fixture commit does not exist in this repository; the gate is not what this test is about.
    monkeypatch.setattr(api, "check_all", lambda items, repo, **kw: [dict(i, check="unchecked", check_reason="test")
                                                                     for i in items])
    s = _sha_store()
    for q in (FULL[:9], FULL, "see commit " + FULL[:9]):
        got = ask(s, q, embed=False)
        assert got and got[0]["statement"].startswith("Merged the coverage sample"), (q, got)
    assert ask(s, "ab12cd34", embed=False)[0]["statement"].startswith("Entity linked change")   # the commit entity


def test_ask_does_not_treat_words_or_timestamps_as_sha():
    s = _store()
    calls = []
    real = s.facts_by_sha
    s.facts_by_sha = lambda t, limit=20: (calls.append(t), real(t, limit))[1]
    ask(s, "defaced 1793353688 facade deadbeef", embed=False)       # hex-only word, digits-only, hex-only word
    assert calls == []


def test_rest_store_facts_by_sha_issues_exact_ref_and_commit_prefix_reads():
    paths = []
    RestStore(request=lambda method, path, body, headers, timeout: paths.append((method, path)) or []).facts_by_sha(FULL)
    refs = [p for m, p in paths if "evidence=cs." in p]
    assert len(refs) == 6 and any(FULL[:9] in p for p in refs) and all("valid_to=is.null" in p for p in refs)
    assert any("brain_entities" in p and "type=eq.commit" in p and "name_lc=like." + FULL + "%2A" in p for m, p in paths)
