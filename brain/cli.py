"""brain: a per-project knowledge graph for AI coding agents.

    brain init                         create .brain/, start the database, apply the schema
    brain boot                         role-free boot (what SessionStart shows)
    brain boot lead                    role boot: orders, open work, last 48 h, doctrine, verify quota
    brain write --role lead --kind rule --entity tool:git --evidence path:README.md \\
                "Never `git stash` in the shared checkout."
    brain verify <id> verified --role reviewer --output-file /path/rerun.txt
    brain retract <id> --role lead --reason-kind misfiled --reason "duplicate task"
    brain handover lead                record a handover episode, print the restart note
    brain install-hook                 add the Claude Code hooks to .claude/settings.json

Exit codes: 0 ok, 2 refused, 3 needs judgement, 4 brain unavailable (a write is queued).

The project is the nearest folder at or above the working directory that holds .brain/config.json
(or BRAIN_ROOT). Values in <project>/.env, .brain/brain.env and the environment that look like
credentials are never stored: a write that contains one is refused."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from brain import EPISODE_KINDS, KINDS, VERDICTS, api, views
from brain.config import DEFAULT, DIR, Config, NoBrain, find_root
from brain.evidence import fetch_once
from brain.gate import EVIDENCE_TYPES, secret_values_from_env, secret_values_from_mapping
from brain.store import BrainRefused, BrainUnavailable, RestStore, VersionConflict

EXIT = {"written": 0, "noop": 0, "refused": 2, "needs_judgement": 3, "queued": 4}
# Not the hooks' 2.5 s cap: a session runs this by hand and can wait out a slow spell.
BOOT_BUDGET_S = 10


class BootTimeout(BaseException):
    """The boot budget ran out. A BaseException on purpose: code that catches Exception around a
    read would otherwise swallow the alarm and the boot would run on."""


def build_parser():
    p = argparse.ArgumentParser(prog="brain", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create .brain/ here, start the database, apply the schema")
    i.add_argument("--roles", help="comma-separated role names (default: lead,builder,reviewer)")
    i.add_argument("--no-start", action="store_true", help="write the files only; do not run Docker")
    sub.add_parser("up", help="start the database (idempotent) and re-apply the schema")
    sub.add_parser("down", help="stop the database containers; the data volume stays")
    sub.add_parser("status", help="show the database containers")
    b = sub.add_parser("boot", help="boot (role-free without ROLE)")
    b.add_argument("role", nargs="?")
    a = sub.add_parser("ask", help="search facts")
    a.add_argument("q")
    e = sub.add_parser("entity", help="facts on one entity")
    e.add_argument("type")
    e.add_argument("name")
    e.add_argument("--json", action="store_true", help="return the entity and its facts as JSON")
    w = sub.add_parser("write", help="write one fact through the gate")
    w.add_argument("--role", required=True)
    w.add_argument("--kind", required=True, choices=KINDS)
    w.add_argument("--operator-order", action="store_true",
                   help="link a decision/settled fact to actor:operator; authority remains unverified")
    w.add_argument("--entity", action="append", default=[], help="TYPE:NAME")
    w.add_argument("--section", help="section slug for any NEW entity (default: the first configured section)")
    w.add_argument("--evidence", action="append", default=[],
                   help=f"TYPE:REF, TYPE one of {', '.join(EVIDENCE_TYPES)}; a path that exists only on an "
                        "unmerged branch passes when the same fact cites that sha")
    w.add_argument("--episode", action="append", default=[])
    w.add_argument("--detail")
    w.add_argument("--judge", action="append", default=[], help="ID=" + "|".join(VERDICTS))
    w.add_argument("statement")
    v = sub.add_parser("verify", help="peer-verify a fact")
    v.add_argument("id")
    v.add_argument("verdict", choices=["verified", "verified_partial", "refuted"])
    v.add_argument("--role", required=True)
    v.add_argument("--output-file", help="the re-run output; without it `verified` lands verified_partial")
    v.add_argument("--note", default="")
    rt = sub.add_parser("retract", help="withdraw a fact: obsolete, misfiled, or a finished task (done)")
    rt.add_argument("id")
    rt.add_argument("--role", required=True)
    rt.add_argument("--reason", required=True)
    rt.add_argument("--reason-kind", choices=("obsolete", "misfiled", "done"), required=True)
    j = sub.add_parser("judge", help="atomically resolve a candidate pair (newer, older)")
    j.add_argument("newer")
    j.add_argument("older")
    j.add_argument("verdict", choices=VERDICTS)
    j.add_argument("--role", required=True)
    q = sub.add_parser("queue", help="facts to verify or judge")
    q.add_argument("--role", required=True)
    q.add_argument("--author", help="only this author's unverified facts")
    ep = sub.add_parser("episode", help="append a provenance episode")
    ep.add_argument("--role", required=True)
    ep.add_argument("--kind", required=True, choices=EPISODE_KINDS)
    ep.add_argument("--occurred-at", help="ISO time the event happened; omit when unknown")
    ep.add_argument("body")
    h = sub.add_parser("handover", help="record a handover and print the restart note")
    h.add_argument("role")
    sub.add_parser("flush", help="replay the offline outbox")
    sub.add_parser("embed", help="embed facts for vector search (needs the optional fastembed package)")
    ih = sub.add_parser("install-hook", help="add the Claude Code hooks to .claude/settings.json")
    ih.add_argument("--dry-run", action="store_true", help="print what would be added; write nothing")
    hk = sub.add_parser("hook", help="run a Claude Code hook (called by Claude Code, not by hand)")
    hk.add_argument("event", choices=["session", "prompt"])
    return p


def _pair(s, sep):
    k, _, v = s.partition(sep)
    if not k.strip() or not v.strip():
        raise SystemExit(f"bad value {s!r}; expected X{sep}Y")
    return k.strip(), v.strip()


def _session():
    return os.getenv("BRAIN_SESSION_ID") or os.getenv("CLAUDE_SESSION_ID")


def draft_from_args(ns, config):
    return {"kind": ns.kind, "statement": ns.statement, "author": ns.role, "detail": ns.detail,
            "section": ns.section or config.sections[0], "operator_order": ns.operator_order,
            "entities": [dict(zip(("type", "name"), _pair(x, ":"))) for x in ns.entity],
            "evidence": [dict(zip(("type", "ref"), _pair(x, ":"))) for x in ns.evidence],
            "episode_ids": list(ns.episode), "author_session": _session(),
            "judgements": dict(_pair(x, "=") for x in ns.judge)}


def _alarm(seconds):
    def raise_timeout(*_):
        raise BootTimeout("boot budget %ss" % seconds)
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.alarm(seconds)


def _init(ns, root):
    from brain import stack
    root = Path(root or Path.cwd()).resolve()
    path = root / DIR / "config.json"
    if path.exists():
        data = json.loads(path.read_text())
    else:
        data = dict(DEFAULT, name=root.name)
        if ns.roles:
            data["roles"] = {r.strip().lower(): "" for r in ns.roles.split(",") if r.strip()}
    if not data.get("rest_port"):
        data["rest_port"] = stack.free_port()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"wrote {DIR}/config.json (roles: {', '.join(data['roles'])})")
    config = Config(root, data)
    for name in stack.write_files(config, data["rest_port"]):
        print(f"wrote {name}")
    if ns.no_start:
        print("Files written. Start the database with `brain up`.")
        return 0
    return _up(config)


def _up(config):
    from brain import stack
    try:
        stack.up(config)
        sections = RestStore(config, timeout=10).ensure_sections(config.sections)
    except (stack.StackError, BrainUnavailable, BrainRefused) as exc:
        print(f"FAILED: {exc}")
        return 4
    print(f"brain ready at {config.url} (sections: {', '.join(s['slug'] for s in sections)}; "
          f"roles: {', '.join(config.roles)})")
    print("Next: `brain install-hook`, then `brain boot <role>`.")
    return 0


def main(argv=None, *, store=None, root=None, fetch=None):
    ns = build_parser().parse_args(argv)
    if ns.cmd == "hook":
        from brain import hook
        return hook.main([ns.event])
    if ns.cmd == "init":
        return _init(ns, root)
    try:
        config = store.config if store is not None else Config.load(find_root(root))
    except NoBrain as exc:
        print(exc)
        return 2
    repo = config.root
    if ns.cmd == "install-hook":
        from brain import hook
        return 0 if hook.install(repo, dry_run=ns.dry_run) else 2
    if ns.cmd in ("up", "down", "status"):
        from brain import stack
        if ns.cmd == "up":
            return _up(config)
        try:
            print(stack.status(config) if ns.cmd == "status" else (stack.down(config) or "stopped (data kept)"))
        except stack.StackError as exc:
            print(f"FAILED: {exc}")
            return 4
        return 0
    do_fetch = (store is None) if fetch is None else fetch
    try:
        if ns.cmd == "boot":
            _alarm(BOOT_BUDGET_S)
            try:
                store = store or RestStore(config)
                print(views.boot(store, ns.role, repo, snapshot=config.snapshot) if ns.role
                      else views.boot_role_free(store, repo, snapshot=config.snapshot))
            except (BrainUnavailable, BootTimeout) as exc:
                print(views._from_snapshot(config.snapshot, ns.role or "role-free", str(exc), views.BOOT_LIMIT))
            finally:
                signal.alarm(0)
            return 0
        store = store or RestStore(config)
        secrets = (secret_values_from_env(repo / ".env") | secret_values_from_env(config.env_file)
                   | secret_values_from_mapping(os.environ))
        if ns.cmd == "write":
            fetched = fetch_once(repo) if do_fetch else True
            d = draft_from_args(ns, config)
            r = api.write_fact(store, d, repo=repo, judgements=d.pop("judgements"), secret_values=secrets,
                               outbox=config.outbox, fetched=fetched)
            print(json.dumps({k: v for k, v in r.items() if k != "fact"}, indent=1))
            if r["status"] == "written":
                print(f"written {r['fact']['id']} [{r['fact']['status']}]")
            if r["status"] == "needs_judgement":
                print("Judge each candidate, then re-run with --judge ID=" + "|".join(VERDICTS))
            if r["status"] == "queued":
                print("The brain did not answer; the write is queued. Run `brain flush` when it is back.")
            return EXIT[r["status"]]
        if ns.cmd == "judge":
            if ns.role not in config.verifiers:
                raise BrainRefused(f"the judge must be one of {', '.join(config.verifiers)}")
            newer = store.get("brain_facts", store.resolve_fact_id(ns.newer) or ns.newer)
            older = store.get("brain_facts", store.resolve_fact_id(ns.older) or ns.older)
            if not newer or not older:
                raise BrainRefused("no such fact")
            print(json.dumps(store.judge_pair(newer, older, ns.verdict, ns.role)))
            return 0
        if ns.cmd in ("verify", "retract"):
            ns.id = store.resolve_fact_id(ns.id) or ns.id
            if store.get("brain_facts", ns.id) is None:
                print("no such fact")
                return 2
            if ns.cmd == "verify":
                out = Path(ns.output_file).read_text() if ns.output_file else ""
                r = api.verify_fact(store, ns.id, ns.verdict, role=ns.role, output=out, note=ns.note,
                                    secret_values=secrets)
            else:
                r = api.retract_fact(store, ns.id, role=ns.role, reason=ns.reason, reason_kind=ns.reason_kind,
                                     secret_values=secrets)
            print(f"{r['id'][:8]} -> {r['status']}")
            return 0
        if ns.cmd == "ask":
            from brain.search import ask
            stats = {}
            for f in ask(store, ns.q, stats=stats):
                print(views.line(f, with_evidence=True))
            if stats.get("vector_only_cut"):
                print(f"(+{stats['vector_only_cut']} vector-only hits not shown; cap {stats['vector_only_cap']} of "
                      f"{stats['vector_only_seen']}; keyword and sha hits are never cut)", file=sys.stderr)
            return 0
        if ns.cmd == "entity":
            from brain.search import _shas
            ent = store.find_entity(ns.type, ns.name)
            by_sha = store.facts_by_sha(ns.name.lower()) if ns.type == "commit" and _shas(ns.name) else []
            if not ent and not by_sha:
                print("no such entity")
                return 2
            facts = (store.find_facts([ent["id"]]) if ent else [])[::-1] + by_sha
            facts = list({f["id"]: f for f in facts}.values())
            if ns.json:
                print(json.dumps({"entity": ent, "facts": facts}, default=str))
                return 0
            if ent:
                print(f"{ent['type']} {ent['name']} aliases={ent['aliases']}")
            for f in facts:
                print(views.line(f, with_evidence=True))
            return 0
        if ns.cmd == "queue":
            secs = [s["id"] for s in store.sections()]
            rows = (store.unverified_queue(ns.role, secs, 50) if not ns.author else
                    [f for f in store.facts_in_sections(secs, None, queue_only=True, limit=500)
                     if f["author"] == ns.author])
            for f in rows:
                print(views.line(f, with_evidence=True))
            return 0
        if ns.cmd == "episode":
            eps = api.add_episode(store, ns.kind, ns.body, author=ns.role, occurred_at=ns.occurred_at,
                                  session_id=_session(), secret_values=secrets)
            print("episode", eps[0]["id"], f"({len(eps)} part(s))")
            return 0
        if ns.cmd == "handover":
            print(views.handover(store, ns.role, repo, session_id=_session()))
            return 0
        if ns.cmd == "flush":
            print(api.flush_outbox(store, config.outbox, repo=repo, secret_values=secrets,
                                   fetched=fetch_once(repo) if do_fetch else True))
            return 0
        if ns.cmd == "embed":
            from brain.embed import embed_texts
            if not config.embeddings:
                print('vector search is off; set "embeddings": true in .brain/config.json first')
                return 2
            done = 0
            while True:
                rows = store.facts_missing_embedding(64)
                if not rows:
                    break
                vectors = embed_texts([(r["statement"] + " " + (r.get("detail") or "")).strip() for r in rows], download=True)
                if vectors is None:
                    print("no embedding model available; install fastembed (pip install fastembed)")
                    return 2
                wrote = store.set_embeddings([{"id": r["id"], "embedding": v} for r, v in zip(rows, vectors)])
                done += wrote
                if not wrote:
                    break
            print(f"embedded {done} fact(s)")
            return 0
    except (BrainRefused, VersionConflict) as exc:
        print(f"REFUSED: {exc}")
        return 2
    except BrainUnavailable as exc:
        print(f"BRAIN UNREACHABLE: {exc}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
