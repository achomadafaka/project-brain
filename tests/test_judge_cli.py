"""`brain judge` resolves short ids, passes the exact pair versions, and refuses unknown roles first."""
from unittest.mock import Mock

import pytest

from brain import api, cli
from conftest import make_config
from fake_store import FakeStore


@pytest.mark.parametrize("role", ["reviewer", "planner", "operator"])
def test_judge_cli_passes_exact_pair_versions(capsys, role):
    store = Mock()
    store.config = make_config()
    store.resolve_fact_id.side_effect = {"new": "new-full", "old": "old-full"}.get
    store.get.side_effect = lambda table, ident: dict(id=ident, version=7 if ident == "new-full" else 4)
    store.judge_pair.return_value = {"verdict": "unrelated", "replayed": False}
    assert cli.main(["judge", "new", "old", "unrelated", "--role", role], store=store) == 0
    store.judge_pair.assert_called_once_with(dict(id="new-full", version=7), dict(id="old-full", version=4),
                                             "unrelated", role)
    assert "unrelated" in capsys.readouterr().out


@pytest.mark.parametrize("role", ["cron", "import", "unknown-role", ""])
def test_judge_cli_refuses_unknown_roles_before_any_read(role):
    store = Mock()
    store.config = make_config()
    assert cli.main(["judge", "new", "old", "supersedes", "--role", role], store=store) == 2
    store.get.assert_not_called()
    store.judge_pair.assert_not_called()


def test_judging_a_pending_pair_clears_the_flag_and_a_changed_verdict_is_refused(tmp_path, capsys):
    s = FakeStore(make_config(tmp_path))
    s.ensure_sections(["general"])
    d = lambda text, author: {"kind": "rule", "statement": text, "author": author, "section": "general",
                              "entities": [{"type": "tool", "name": "git"}],
                              "evidence": [{"type": "query", "ref": "git stash list"}]}
    old = api.write_fact(s, d("Use `git stash` to park the fix.", "lead"), repo=tmp_path)["fact"]
    new = api.write_fact(s, d("Never `git stash` in the shared checkout.", "builder"), repo=tmp_path,
                         interactive=False)["fact"]
    assert new["judge_pending"] is True
    assert cli.main(["judge", new["id"][:8], old["id"][:8], "supersedes", "--role", "reviewer"], store=s) == 0
    assert s.get("brain_facts", new["id"])["judge_pending"] is False
    assert s.get("brain_facts", old["id"])["status"] == "superseded"
    assert cli.main(["judge", new["id"], old["id"], "supersedes", "--role", "reviewer"], store=s) == 0   # replay
    assert cli.main(["judge", new["id"], old["id"], "contradicts", "--role", "reviewer"], store=s) == 2
    assert "judgement_conflict" in capsys.readouterr().out
