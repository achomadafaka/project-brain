# project-brain: design note

A per-project knowledge graph for AI coding agents. Sessions write facts with
evidence, a different role verifies them, and a new session boots with what
earlier sessions learned. Extracted from a working system; this note covers
what the standalone version keeps, drops and changes.

## Storage (decided)

Postgres with pgvector behind PostgREST, in Docker, one stack per project.
The alternative was a SQLite file per project (zero setup, no vector search,
rules re-implemented in Python). Postgres was chosen: it is what the source
system runs, so the schema, triggers and atomic database functions carry over
unchanged in behaviour.

- `brain init` writes `.brain/docker-compose.yml` and starts two containers:
  `db` (pgvector/pgvector:pg17) and `rest` (PostgREST). REST binds to
  `127.0.0.1` on a free port chosen at init; the database port is not published.
- The compose project is named `brain-<folder>-<hash of the path>`, so every
  project has its own containers, its own named volume and its own secrets.
- Secrets are generated into `.brain/brain.env` (mode 600, ignored by git) and
  never printed. The client authenticates with a `service_role` token; the
  schema denies everything to anonymous requests.
- The schema (`brain/schema.sql`) is applied with `psql` inside the db
  container. Every statement is idempotent, so `brain up` re-applies it.
- The source put an nginx gateway in front of PostgREST to imitate a hosted
  API path. Nothing here needs that path, so the gateway is gone.

## Layout

```
brain/
  __init__.py     kinds, statuses, verdicts
  config.py       find the project root, load .brain/config.json
  schema.sql      tables, triggers, database functions
  stack.py        compose file, secrets, start/stop, apply the schema
  store.py        RestStore: PostgREST over HTTP (urllib)
  gate.py         draft validation and the secret gate
  evidence.py     machine checks for sha / path / knob / column evidence
  contradict.py   contradiction candidates (shared entity + shared key)
  api.py          write_fact, verify_fact, retract_fact, add_episode, outbox
  search.py       ask: commit SHA + full-text (+ optional vector), recency, one-hop expansion
  embed.py        optional embeddings
  views.py        boot and handover text
  hook.py         Claude Code SessionStart / UserPromptSubmit hook + installer
  cli.py          the `brain` command
tests/            pytest; fake_store.py mirrors the database rules for offline tests
```

## Model (unchanged from the source)

- **Fact**: one claim, 8..600 chars, with a kind (rule, trap, decision,
  settled, finding, measurement, task, observation), at least one entity, and
  evidence. Statement, kind, author and entities are immutable; a correction
  is a new fact that supersedes the old one. Nothing is deleted.
- **Evidence gate**: every non-observation fact cites evidence. `sha`, `path`,
  `knob` and `column` items are checked against the git repository; a
  reference the check proves false refuses the write.
- **Secret gate**: text containing a value from `.env`, `.brain/brain.env`,
  the environment, or a credential shape (JWT, keyed URL, `sk_live_...`,
  `password=...`) is refused, value never shown.
- **Peer verify**: a fact is verified by a role other than its author.
  `verified` needs re-run output, otherwise it lands `verified_partial`.
- **Contradictions**: a new fact that shares an entity and a key with a live
  fact of the same kind group returns `needs_judgement`; the writer judges
  each candidate as duplicate / refines / supersedes / contradicts / unrelated.
  The fact, the lifecycle changes and the links commit in one transaction.
- **Episodes**: append-only provenance in parts of 1,800 chars.
- **Outbox**: a write that cannot reach the database is appended to a locked
  JSONL file with a stable id and replayed by `brain flush`.
- **Expiry**: `verify_by` by kind (measurement 7 days, task 3, settled 60,
  others 30). Stale and broken-evidence facts are held back from the boot.

## Changes from the source

- **Roles are free-form**, listed in `.brain/config.json` with a one-line
  description each. `operator` (the human) is always a valid author and
  verifier. The database no longer holds a role list; the client checks it.
- **Entity types and sections come from config**, with generic defaults.
  `--section` defaults to the first configured section.
- **One role view**: a role's boot reads every section. Per-role section
  filters were a table in the source; add them when a project needs them.
- **Boot shows a one-line git state** (branch, HEAD, dirty count) in place of
  the source's derived live-system state.
- **Ranked search in the database** (`brain_search_facts`): every query word
  must match; if nothing does, any two words. The source's REST full-text read
  was unranked and all-words only, so a question in prose matched nothing.
- **Vector search is opt-in** (`"embeddings": true` plus the optional
  `fastembed` package). A query never downloads a model.
- **`retract --reason-kind done`** closes a finished task; the source had a
  separate work-record flow for that.
- **Hook** is `brain hook session|prompt`, installed by `brain install-hook`,
  which prints the JSON it will merge into `.claude/settings.json` and then
  writes it (`--dry-run` prints only). The hook always exits 0.
- **Evidence checks work without a remote**: a repository with no
  `origin/main` is checked against HEAD.

## Left out

Live-system state, document importers, the messaging bridge, nightly jobs,
work-coverage and closure-audit, hooks for other agent harnesses, per-role section filters,
backups (use `pg_dump` in the db container).

## CLI

`brain init | up | down | status | boot [role] | ask | entity | write | verify
| retract | judge | queue | episode | handover | flush | embed | install-hook
| hook`

Exit codes: 0 ok, 2 refused, 3 needs judgement, 4 brain unavailable (write queued).

## Tests

pytest, as in the source. Offline tests run against `tests/fake_store.py`,
which mirrors the database rules. `tests/test_live.py` runs `brain init` and
the same rules against the real database; it starts Docker and is skipped
unless `BRAIN_LIVE_TEST=1`.
