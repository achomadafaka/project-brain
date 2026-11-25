"""Project root discovery and .brain/config.json.

A project either owns its database (`brain init` writes a compose file and `rest_port`) or points at
one that already exists: "url" names the REST address, and "key_file" + "key_name" say where its
service key is kept. The key is read at run time and never copied into the project.

Roles are free-form names. `operator` (the human) is always a valid author and verifier, and the
entity types `actor`, `tool` and `commit` always exist because the write path uses them."""
from __future__ import annotations

import json
import os

from brain import EPISODE_KINDS
from pathlib import Path

DIR = ".brain"
OPERATOR = "operator"
DEFAULT = {
    "roles": {"lead": "Leads the work, integrates and pushes.",
              "builder": "Implements changes.",
              "reviewer": "Reviews and measures, read-only."},
    "sections": ["general"],
    "entity_types": ["actor", "tool", "commit", "path", "process", "service", "component", "table",
                     "column", "knob", "spec", "task", "release", "concept"],
    "main_ref": "origin/main",
    "embeddings": False,
}
_REQUIRED_TYPES = ("actor", "tool", "commit")


class NoBrain(RuntimeError):
    pass


def find_root(start=None) -> Path:
    """The nearest ancestor of `start` (default: BRAIN_ROOT, else the working directory) holding .brain/."""
    here = Path(start or os.getenv("BRAIN_ROOT") or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / DIR / "config.json").is_file():
            return d
    raise NoBrain(f"no {DIR}/config.json in {here} or above; run `brain init`")


def read_env(path) -> dict:
    """KEY=VALUE lines of a file; a missing file is empty."""
    out = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Config:
    def __init__(self, root, data=None):
        self.root = Path(root)
        self.data = data = {**DEFAULT, **(data or {})}
        roles = data["roles"]
        if isinstance(roles, list):
            roles = {r: "" for r in roles}
        self.roles = {str(k).strip().lower(): str(v) for k, v in roles.items() if str(k).strip()}
        self.sections = [str(s) for s in data["sections"]] or ["general"]
        self.entity_types = tuple(dict.fromkeys([*_REQUIRED_TYPES, *data["entity_types"]]))
        self.main_ref = data["main_ref"]
        self.name = data.get("name") or self.root.name
        self.rest_port = data.get("rest_port")
        self.embeddings = bool(data.get("embeddings", False))
        self.external_url = data.get("url")              # a database this project does not manage
        self.key_file, self.key_name = data.get("key_file"), data.get("key_name") or "BRAIN_SERVICE_KEY"
        self.episode_kinds = tuple(data.get("episode_kinds") or EPISODE_KINDS)
        # Episode kinds the boot lists under LAST 48 H.
        self.recent_kinds = tuple(data.get("recent_kinds") or ("handoff_line", "handover", "operator_order", "report"))

    @classmethod
    def load(cls, root):
        return cls(root, json.loads((Path(root) / DIR / "config.json").read_text()))

    @property
    def verifiers(self):
        return tuple(dict.fromkeys([*self.roles, OPERATOR]))

    authors = verifiers

    @property
    def dir(self):
        return self.root / DIR

    @property
    def env_file(self):
        return self.dir / "brain.env"

    @property
    def outbox(self):
        return self.dir / "outbox.jsonl"

    @property
    def snapshot(self):
        return self.dir / "snapshot.json"

    @property
    def url(self):
        return (os.getenv("BRAIN_URL") or self.external_url or f"http://127.0.0.1:{self.rest_port}").rstrip("/")

    def service_key(self):
        if os.getenv("BRAIN_SERVICE_KEY"):
            return os.environ["BRAIN_SERVICE_KEY"]
        if self.key_file:
            return read_env(Path(self.key_file).expanduser()).get(self.key_name) or ""
        return read_env(self.env_file).get("BRAIN_SERVICE_KEY") or ""
