"""The per-project database: Postgres with pgvector behind PostgREST, run by Docker Compose.

`brain init` writes three files into .brain/ and starts the stack:
  docker-compose.yml  two containers (db, rest) and one named volume; REST binds to 127.0.0.1 only
  brain.env           generated secrets, mode 600, ignored by git, never printed
  .gitignore          keeps brain.env, the outbox, the snapshot and hook state out of the repository
The database port is not published; the schema is applied with psql inside the db container."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from brain.config import DIR, read_env

SCHEMA = Path(__file__).with_name("schema.sql")
GITIGNORE = "brain.env\noutbox.jsonl*\nsnapshot.json*\nstate/\n"
COMPOSE = """\
# Written by `brain init`. The project brain: Postgres + pgvector (db) behind PostgREST (rest).
# Secrets are in brain.env beside this file (ignored by git). REST listens on 127.0.0.1 only.
name: {project}

services:
  db:
    image: pgvector/pgvector:pg17
    restart: unless-stopped
    env_file: brain.env
    environment:
      POSTGRES_DB: postgres
      POSTGRES_USER: postgres
    volumes:
      - pg:/var/lib/postgresql/data
    shm_size: 128mb
    healthcheck:
      # TCP, not the socket: the first-boot init server listens on the socket only.
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U postgres -d postgres"]
      interval: 3s
      timeout: 3s
      retries: 40

  rest:
    image: postgrest/postgrest:latest
    restart: unless-stopped
    env_file: brain.env
    environment:
      PGRST_DB_SCHEMAS: public
      PGRST_DB_ANON_ROLE: anon
      PGRST_DB_EXTRA_SEARCH_PATH: public,extensions
      PGRST_DB_MAX_ROWS: "1000"
      PGRST_DB_POOL: "10"
    ports:
      - "127.0.0.1:{port}:3000"
    depends_on:
      db:
        condition: service_healthy

volumes:
  pg:
"""


class StackError(RuntimeError):
    pass


def project_name(root) -> str:
    """A compose project name unique to this folder: brain-<folder>-<6 hex of the path>."""
    root = Path(root).resolve()
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "project"
    return f"brain-{slug[:30]}-{hashlib.sha256(str(root).encode()).hexdigest()[:6]}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_jwt(secret: str, role: str) -> str:
    """An HS256 token PostgREST accepts for `role`. No expiry: the stack only listens on localhost."""
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"role": role, "iss": "project-brain"}).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def write_files(config, port) -> list[str]:
    """Create the compose file, the secrets and the gitignore when missing. Returns what was written.
    Existing secrets are kept: the database volume was initialised with them."""
    d = Path(config.root) / DIR
    d.mkdir(parents=True, exist_ok=True)
    wrote = []
    if not (d / "brain.env").exists():
        pg, auth, jwt = (secrets.token_urlsafe(32) for _ in range(3))
        fd = os.open(d / "brain.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"POSTGRES_PASSWORD={pg}\nAUTHENTICATOR_PASSWORD={auth}\n"
                     f"PGRST_DB_URI=postgres://authenticator:{auth}@db:5432/postgres\n"
                     f"PGRST_JWT_SECRET={jwt}\nBRAIN_SERVICE_KEY={mint_jwt(jwt, 'service_role')}\n")
        wrote.append(f"{DIR}/brain.env")
    for name, text in (("docker-compose.yml", COMPOSE.format(project=project_name(config.root), port=port)),
                       (".gitignore", GITIGNORE)):
        if not (d / name).exists():
            (d / name).write_text(text)
            wrote.append(f"{DIR}/{name}")
    return wrote


def _compose(config, *args, stdin=None, timeout=300):
    cmd = ["docker", "compose", "-f", str(Path(config.root) / DIR / "docker-compose.yml"), *args]
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise StackError("docker is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise StackError(f"`docker compose {' '.join(args[:3])}` timed out after {timeout}s") from None


def _checked(config, *args, **kw):
    r = _compose(config, *args, **kw)
    if r.returncode != 0:
        raise StackError(f"`docker compose {' '.join(args[:3])}` failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def up(config, wait_s=90):
    """Start the stack, apply the schema (idempotent) and wait until REST answers with the service key."""
    _checked(config, "up", "-d", "--wait", "db")
    auth = read_env(config.env_file).get("AUTHENTICATOR_PASSWORD", "")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,}", auth):
        raise StackError(f"{DIR}/brain.env has no usable AUTHENTICATOR_PASSWORD")
    # The password travels on stdin, never on a command line.
    sql = SCHEMA.read_text() + f"\nALTER ROLE authenticator PASSWORD '{auth}';\nNOTIFY pgrst, 'reload schema';\n"
    _checked(config, "exec", "-T", "db", "psql", "-U", "postgres", "-d", "postgres", "-q",
             "-v", "ON_ERROR_STOP=1", "-f", "-", stdin=sql)
    _checked(config, "up", "-d", "rest")
    key, deadline, last = config.service_key(), time.monotonic() + wait_s, "no answer"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"{config.url}/brain_sections?select=id&limit=1",
                                         headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last = type(exc).__name__ + (f" {exc.code}" if hasattr(exc, "code") else "")
        time.sleep(1)
    raise StackError(f"REST did not answer on {config.url} within {wait_s}s ({last})")


def down(config):
    """Stop the containers. The data volume stays."""
    _checked(config, "down")


def status(config) -> str:
    return _checked(config, "ps", "--format", "table {{.Service}}\t{{.State}}\t{{.Status}}").stdout.strip()
