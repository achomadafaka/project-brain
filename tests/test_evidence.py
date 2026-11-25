"""Machine evidence checks. A throwaway git repo makes on-main / local-only / branch-only / tag-only
commits, template knobs and ALTER-added columns deterministic."""
import subprocess
from pathlib import Path

import pytest

from brain.evidence import check, check_all, fetch_once


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, rel, text, msg):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(repo, "add", rel)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    on_main = _commit(r, "app/settings/knobs.py", "\n".join([
        'APP_LIVE_TP_PCT = os.getenv("APP_LIVE_TP_PCT", "25")',
        'url = os.getenv("APP_REGION_READ_URL_%s" % REGION)',
        'g = os.getenv(f"APP_X_GRACE_{region}")',
        'CREATE TABLE IF NOT EXISTS job_runs (region_id TEXT, block INTEGER)',
        'ALTER TABLE job_runs ADD COLUMN block_hash TEXT',
    ]), "base")
    _commit(r, "rust/worker/src/main.rs", 'let p = std::env::var("APP_WORKER_PORT");', "rust")
    _commit(r, "tests/test_k.py", 'assert "ZZQT_ONLY_NAMED_IN_A_TEST" and "APP_LIVE_ONLY_IN_TEST"', "test")
    _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")           # what origin has
    local = _commit(r, "docs/new_audit.md", "x", "local, not pushed")
    _git(r, "checkout", "-q", "-b", "side")
    side = _commit(r, "side.txt", "x", "on a side branch only")
    _git(r, "checkout", "-q", "main")
    _git(r, "checkout", "-q", "-b", "tmp")
    tagged = _commit(r, "tagged.txt", "x", "tag only")
    _git(r, "tag", "v-old", tagged)
    _git(r, "checkout", "-q", "main")
    _git(r, "branch", "-q", "-D", "tmp")
    return {"repo": r, "on_main": on_main, "local": local, "side": side, "tagged": tagged}


def _v(item, r, **kw):
    return check(item, r["repo"], **kw)[0]


def test_sha_states(repo):
    assert _v({"type": "sha", "ref": repo["on_main"][:10]}, repo) == "ok"
    assert _v({"type": "sha", "ref": repo["local"][:10]}, repo) == "pending_push"      # HEAD, not pushed
    assert _v({"type": "sha", "ref": repo["side"][:10]}, repo) == "pending_push"       # a side branch
    assert _v({"type": "sha", "ref": repo["tagged"][:10]}, repo) == "broken"            # tag only: never merged
    assert _v({"type": "sha", "ref": "0123456789abcdef0123"}, repo) == "broken"
    assert _v({"type": "sha", "ref": "0123456789abcdef0123"}, repo, fetched=False) == "unchecked"
    assert _v({"type": "sha", "ref": "not-a-sha"}, repo) == "broken"


def test_paths(repo):
    assert _v({"type": "path", "ref": "app/settings/knobs.py"}, repo) == "ok"
    assert _v({"type": "path", "ref": "app/settings/knobs.py:3"}, repo) == "ok"          # file:line
    assert _v({"type": "doc", "ref": "docs/new_audit.md"}, repo) == "pending_push"
    assert _v({"type": "path", "ref": "no/such/file.py"}, repo) == "broken"
    assert _v({"type": "path", "ref": "no/such/file.py"}, repo, fetched=False) == "unchecked"
    assert _v({"type": "path", "ref": "data/runner.pid"}, repo) == "unchecked"              # runtime, absent
    (repo["repo"] / "data").mkdir()
    (repo["repo"] / "data" / "runner.pid").write_text("1")
    assert _v({"type": "path", "ref": "data/runner.pid"}, repo) == "ok"


def test_knobs(repo):
    assert _v({"type": "knob", "ref": "APP_LIVE_TP_PCT"}, repo) == "ok"
    assert _v({"type": "knob", "ref": "APP_REGION_READ_URL_56"}, repo) == "ok"   # "..._%s" % REGION
    assert _v({"type": "knob", "ref": "APP_X_GRACE_7"}, repo) == "ok"                # f"..._{region}"
    assert _v({"type": "knob", "ref": "APP_WORKER_PORT"}, repo) == "ok"             # Rust reads env
    assert _v({"type": "knob", "ref": "APP_LIVE_TP_PCTX"}, repo) == "unchecked"         # family known
    assert _v({"type": "knob", "ref": "ZZZQ_TOTALLY_MADE_UP"}, repo) == "broken"
    assert _v({"type": "knob", "ref": "lower_case"}, repo) == "broken"
    assert _v({"type": "knob", "ref": "ZZQT_ONLY_NAMED_IN_A_TEST"}, repo) == "broken"      # tests do not define
    assert _v({"type": "knob", "ref": "APP_LIVE_ONLY_IN_TEST"}, repo) == "unchecked"  # family is real


def test_columns(repo):
    assert _v({"type": "column", "ref": "job_runs.block"}, repo) == "ok"                  # CREATE TABLE
    assert _v({"type": "column", "ref": "job_runs.block_hash"}, repo) == "ok"             # ALTER TABLE ADD
    assert _v({"type": "column", "ref": "job_runs.not_found"}, repo) == "unchecked"        # never broken
    assert _v({"type": "column", "ref": "elsewhere.status"}, repo) == "unchecked"            # external table


def test_check_all_annotates_and_never_raises(repo):
    out = check_all([{"type": "query", "ref": "select 1"}, {"type": "knob", "ref": "APP_LIVE_TP_PCT"}],
                    repo["repo"])
    assert [o["check"] for o in out] == ["unchecked", "ok"] and all(o["check_reason"] for o in out)
    assert check({"type": "sha", "ref": "abcdef1"}, Path("/nonexistent/repo"))[0] in ("unchecked", "broken")


def test_a_path_only_at_the_cited_sha_is_pending_push_not_broken(repo):
    """A file on an unmerged branch passes when the fact also cites the branch commit."""
    gone = {"type": "path", "ref": "side.txt"}
    assert check_all([gone], repo["repo"])[0]["check"] == "broken"
    both = check_all([gone, {"type": "sha", "ref": repo["side"][:10]}], repo["repo"])
    assert both[0]["check"] == "pending_push" and "cited commit" in both[0]["check_reason"]


def test_a_repository_without_a_remote_is_checked_against_head(tmp_path):
    r = tmp_path / "local"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    sha = _commit(r, "README.md", "APP_LOCAL_SETTING = 1", "first")
    assert fetch_once(r) is True                                      # no origin: nothing to fetch
    out = check_all([{"type": "path", "ref": "README.md"}, {"type": "sha", "ref": sha[:10]},
                     {"type": "path", "ref": "missing.py"}], r, fetched=True)
    assert [o["check"] for o in out] == ["ok", "ok", "broken"]


def test_a_folder_that_is_not_a_repository_never_refuses(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    assert fetch_once(tmp_path) is False
    out = check_all([{"type": "path", "ref": "notes.md"}, {"type": "path", "ref": "missing.py"},
                     {"type": "sha", "ref": "abcdef1234"}], tmp_path, fetched=False)
    assert [o["check"] for o in out] == ["pending_push", "unchecked", "unchecked"]
