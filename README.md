# project-brain

A dedicated, local knowledge graph for the AI coding agents that work on one
project. Agents write **facts** with **evidence**, a different role
**verifies** them, and every new session **boots** with what earlier sessions
learned: the human's standing orders, open work, the last 48 hours, and the
rules and traps whose evidence still holds.

- One brain per project, in `.brain/`, backed by Postgres + pgvector behind
  PostgREST in Docker. Local only: it listens on `127.0.0.1`.
- Standard library only (Python 3.11+). No Python dependencies.
- A fact needs evidence the tool can check (a commit, a path, a setting name).
  A reference that is provably false refuses the write.
- A value that looks like a secret is never stored.
- Nothing is deleted. A correction supersedes; a withdrawal retracts.

Design and what was left out: [DESIGN.md](DESIGN.md).

## Requirements

- Python 3.11 or newer
- Docker with the Compose plugin (`docker compose version`)
- git (evidence is checked against the project's repository)
- Linux or macOS (the offline outbox uses `fcntl` file locks)

## Install

```bash
pip install -e /path/to/project-brain
```

This puts a `brain` command on your PATH. Without installing, run it as
`PYTHONPATH=/path/to/project-brain python3 -m brain ...`.

## Start a brain in a project

```bash
cd ~/code/my-project
brain init                 # or: brain init --roles lead,builder,reviewer
brain install-hook         # optional: Claude Code hooks, see below
brain boot lead
```

`brain init` creates:

| File | What it is | Commit it? |
|---|---|---|
| `.brain/config.json` | roles, sections, entity types, the REST port | yes |
| `.brain/docker-compose.yml` | the two containers and the data volume | yes |
| `.brain/.gitignore` | ignores the files below | yes |
| `.brain/brain.env` | generated secrets (mode 600) | never |
| `.brain/outbox.jsonl`, `snapshot.json`, `state/` | queue, last boot, hook state | no |

It then starts the stack, applies the schema and prints the local URL. The
first run pulls two Docker images. Running `brain init` or `brain up` again is
safe: existing secrets and data are kept.

The data lives in a Docker volume owned by the compose project named in
`.brain/docker-compose.yml`. The name is fixed at init, so the data stays
attached when the project folder is moved or renamed, as long as `.brain/`
moves with it. Back up with `pg_dump` inside the `db` container.

### Using a database that already exists

A project can point at a brain database it does not manage instead of running
its own stack. Write `.brain/config.json` by hand, without `brain init`:

```json
{
  "url": "http://127.0.0.1:54321/rest/v1",
  "key_file": "~/.config/my-stack/client.env",
  "key_name": "SERVICE_KEY",
  "roles": {"lead": "Leads the work."},
  "sections": ["general"]
}
```

The service key is read from `key_file` at run time and is never copied into
the project. `brain init`, `up`, `down` and `status` refuse to run for such a
project: the stack is started, stopped and migrated wherever it is managed.
The database needs the tables and functions of `brain/schema.sql`. If its
episode kinds or entity types differ from the defaults, list them under
`episode_kinds`, `recent_kinds` (the kinds a boot shows under LAST 48 H) and
`entity_types`.

## Roles

Roles are free-form names in `.brain/config.json`:

```json
{
  "roles": {
    "lead": "Leads the work, integrates and pushes.",
    "builder": "Implements changes.",
    "reviewer": "Reviews and measures, read-only."
  }
}
```

Every write names its role (`--role`). `operator` always exists and means the
human. A fact is verified by a role other than the one that wrote it. Role
names are self-declared: this is an audit trail, not access control.

## Commands

| Command | What it does |
|---|---|
| `brain init [--roles a,b] [--no-start]` | Create `.brain/`, start the database, apply the schema. |
| `brain up` / `down` / `status` | Start (idempotent), stop (data kept), show the containers. |
| `brain boot` | Role-free boot: standing orders, disputed facts, queue size. |
| `brain boot <role>` | Role boot: orders, open work, last 48 h, role doctrine, traps and rules, verify quota. |
| `brain ask "<words or commit sha>"` | Search live facts. |
| `brain entity <type> <name> [--json]` | Every live fact on one entity. |
| `brain write ...` | Write one fact through the gate. |
| `brain verify <id> verified\|verified_partial\|refuted --role R [--output-file F] [--note N]` | Peer-verify. |
| `brain retract <id> --role R --reason-kind obsolete\|misfiled\|done --reason "..."` | Withdraw a fact or close a task. |
| `brain judge <newer> <older> <verdict> --role R` | Resolve a contradiction candidate pair. |
| `brain queue --role R [--author A]` | Facts waiting for verification or judgement. |
| `brain episode --role R --kind K "<body>"` | Append provenance (a handoff line, a report, command output). |
| `brain handover <role>` | Record a handover and print a paste-ready restart note. |
| `brain flush` | Replay writes queued while the database was down. |
| `brain install-hook [--dry-run]` | Add the Claude Code hooks to `.claude/settings.json`. |

Fact ids can be given as the 8-character prefix the boot prints.

Exit codes: `0` ok, `2` refused, `3` needs judgement, `4` brain unavailable
(a write was queued to the outbox).

### Writing a fact

```bash
brain write --role lead --kind rule \
  --entity tool:git --evidence path:CONTRIBUTING.md \
  'Never `git stash` in the shared checkout.'
```

- `--kind`: `rule`, `trap`, `decision`, `settled`, `finding`, `measurement`,
  `task`, `observation`.
- `--entity TYPE:NAME` (repeatable): what the fact is about. Types come from
  `entity_types` in the config. A role's own doctrine and tasks go on
  `actor:<role>`; they lead that role's boot.
- `--evidence TYPE:REF` (repeatable, required except for observations):

  | Type | Checked how |
  |---|---|
  | `sha` | the commit exists and is on the main ref (ok) or a local branch (pending_push) |
  | `path`, `doc` | the file exists at the main ref, at HEAD, or on disk |
  | `knob` | a setting or environment variable name that source code mentions |
  | `column` | `table.column` found in a CREATE TABLE or ALTER TABLE in source |
  | `query`, `episode`, a `doc` URL | not machine-checked; shown as `[unchecked]` for the verifier |

- `--operator-order`: a decision the human gave. It is linked to
  `actor:operator` and listed first in every boot, marked
  `authenticated:false` because the brain cannot prove who said it.

If the new fact shares an entity and a key (backticked code, a SETTING_NAME, a
known action such as `git stash`) with a live fact, the write stops with exit
code 3 and lists the candidates. Judge each one and re-run:

```bash
brain write ... --judge 92fb78b2-...=supersedes 'Do not `git stash`; park work on a branch.'
```

Verdicts: `duplicate` (nothing is written), `refines`, `supersedes` (the old
fact is closed), `contradicts` (both become `disputed` until resolved),
`unrelated`.

### Verifying

```bash
pytest tests/test_parser.py > /tmp/rerun.txt
brain verify 02f41f10 verified --role reviewer --output-file /tmp/rerun.txt
```

Without `--output-file`, `verified` is recorded as `verified_partial`. A
refutation needs a note or output. Facts expire: measurements after 7 days,
tasks after 3, settled facts after 60, everything else after 30. Expired and
broken-evidence facts are held back from boots and counted instead.

## The Claude Code hook

```bash
brain install-hook --dry-run   # print what would be added
brain install-hook             # print it, then merge it into .claude/settings.json
```

It adds two hooks and keeps everything else in the file:

- **SessionStart** runs `brain hook session`: a role-free boot.
- **UserPromptSubmit** runs `brain hook prompt`: the first prompt that says
  "You are &lt;role&gt;" gets that role's boot, once per session. Any other prompt
  of 40+ characters gets up to five matching facts that this session has not
  been shown yet.

Everything a hook prints is wrapped in a labelled block and marked as data,
not instructions. A hook has a 2.5 second budget, falls back to the last boot
snapshot when the database is slow or down, and always exits 0, so it can
never block a prompt. Set `BRAIN_AMBIENT_ENABLED=0` to silence it.

If `brain` is on your PATH the hook command is `brain hook ...`. Otherwise the
installer writes this checkout's absolute path into the command; re-run
`brain install-hook` after removing the old entries if you move the checkout.

## The secret gate

Before anything is stored, the statement, detail, evidence, verification
output, retraction reason or episode body is scanned for:

- values from `<project>/.env`, `.brain/brain.env` and the process
  environment whose names look like credentials (`*_KEY`, `*_TOKEN`,
  `*_SECRET`, `*_PASSWORD`, ...), and the key-like parts of configured URLs;
- credential shapes: JWTs, keyed URLs (`/v2/<key>`, `?api-key=`), `sk_live_`
  style keys, `password=...` pairs, raw keypair byte arrays.

A hit refuses the write. The value is never echoed. A public URL or a short
key id is not a secret and passes.

## Optional: vector search

`ask` uses commit lookup and ranked full-text search. To add vector search,
set `"embeddings": true` in `.brain/config.json`, `pip install fastembed`, and
run `brain embed` (it may download a small embedding model once). Vector-only
hits always rank below keyword hits.

## Environment variables

| Variable | Effect |
|---|---|
| `BRAIN_ROOT` | Use this project instead of searching up from the working directory. |
| `BRAIN_URL`, `BRAIN_SERVICE_KEY` | Override the REST address and key. |
| `BRAIN_AMBIENT_ENABLED=0` | Turn the hooks off. |

## Tests

```bash
python3 -m pytest                                   # offline, no Docker
BRAIN_LIVE_TEST=1 python3 -m pytest tests/test_live.py   # real database; starts and removes containers
```

## License

MIT. See [LICENSE](LICENSE).
