"""Pure checks on a fact draft or an episode body. No I/O except reading env files for secret values.

The secret scan refuses the SECRET, not the endpoint. A public URL is configuration that notes quote
on purpose. Only the key-like parts of a configured URL (a /v2/<key> path segment, an ?api-key=<uuid>
query value, a path token) count as secret values. Treating every *_URL value as secret would refuse
every note that names a public host. Interactive writes are REFUSED on a secret; `redact` exists for
callers that must keep a body as provenance and lose only the secret."""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from brain import KINDS

EVIDENCE_TYPES = ("sha", "path", "knob", "column", "doc", "query", "episode")
# Name TOKENS (split on `_`), so BYPASS and PASSIVE are not PASS and KEYPAIR_PATH is not a key.
_SECRET_TOKENS = {"KEY", "KEYS", "SECRET", "TOKEN", "PASSWORD", "PASS", "PRIVATE", "SEED", "MNEMONIC",
                  "APIKEY", "AUTH", "JWT"}
_URL_TOKENS = {"URL", "URLS", "RPC", "WS", "ENDPOINT"}
_KEYLIKE = re.compile(r"[A-Za-z0-9_\-]{16,}")
_SECRET_SHAPES = (
    re.compile(r"https?://\S*?(?:/v2/|/v3/|api[-_]?key=|apikey=|token=|access[-_]?token=)[A-Za-z0-9_\-]{16,}", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),          # JWT
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{12,}"),
    # key=value with a credential-looking value: letters AND digits, 16+ chars
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\s*[=:]\s*"
               r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]"),                     # a raw 64-byte keypair array
)
_REFUSAL = "refused: the text contains a secret or a keyed URL (value not shown)"


def _keylike_parts(url: str) -> set[str]:
    """Path segments and query values of a URL that look like credentials (16+ key characters with
    a digit), never the host. A public endpoint yields nothing."""
    parts = set()
    try:
        u = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return parts
    for seg in u.path.split("/"):
        if _KEYLIKE.fullmatch(seg) and re.search(r"\d", seg):
            parts.add(seg)
    for _, v in urllib.parse.parse_qsl(u.query):
        if _KEYLIKE.fullmatch(v) and re.search(r"\d", v):
            parts.add(v)
    return parts


def secret_values_from_env(path: Path) -> set[str]:
    """Values the text of a fact or an episode must never contain. Never printed."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return set()
    out = set()
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            # Keep every configured value, including shadowed credentials in command output.
            out |= secret_values_from_mapping({name: value.strip().strip('"').strip("'")})
    return out


def secret_values_from_mapping(values) -> set[str]:
    """Scan an environment mapping without mutating it or exposing credential values."""
    out: set[str] = set()
    for name, value in values.items():
        tokens = set(name.strip().upper().split("_"))
        if tokens & _URL_TOKENS or "://" in value:
            for url in value.split(","):
                out |= _keylike_parts(url)
        elif tokens & _SECRET_TOKENS and len(value) >= 12:
            out.add(value)
    return out


def contains_secret(text: str, secret_values) -> bool:
    text = text or ""
    return any(v and v in text for v in secret_values) or any(p.search(text) for p in _SECRET_SHAPES)


def redact(text: str, secret_values) -> tuple[str, int]:
    """(text with every secret value and credential shape replaced by [REDACTED], count). For the
    callers that keep provenance: the body stays and loses only the secret."""
    n = 0
    for v in sorted((v for v in secret_values if v), key=len, reverse=True):
        if v in text:
            n += text.count(v)
            text = text.replace(v, "[REDACTED]")
    for p in _SECRET_SHAPES:
        text, k = p.subn("[REDACTED]", text)
        n += k
    return text, n


def validate_draft(draft: dict, *, secret_values, authors, entity_types) -> list[str]:
    errs = []
    kind = draft.get("kind")
    if kind not in KINDS:
        errs.append(f"kind must be one of {', '.join(KINDS)}")
    st = (draft.get("statement") or "").strip()
    if not 8 <= len(st) <= 600:
        errs.append("statement must be 8..600 chars (one claim; longer material is an episode or a detail)")
    if len(draft.get("detail") or "") > 4096:
        errs.append("detail must be <= 4096 chars")
    author = draft.get("author")
    if author not in authors:
        errs.append(f"author must be one of {', '.join(authors)}")
    ents = draft.get("entities") or []
    if not ents:
        errs.append("at least one entity is required")
    for e in ents:
        if e.get("type") not in entity_types:
            errs.append(f"entity type {e.get('type')!r} is not a configured type ({', '.join(entity_types)})")
        if not (e.get("name") or "").strip():
            errs.append("an entity needs a name")
    if kind == "observation":
        if not draft.get("episode_ids"):
            errs.append("an observation needs an episode_id")
    elif not draft.get("evidence"):
        errs.append("evidence required: " + ", ".join(EVIDENCE_TYPES))
    for ev in draft.get("evidence") or []:
        if ev.get("type") not in EVIDENCE_TYPES or not str(ev.get("ref") or "").strip():
            errs.append(f"evidence items need a type in ({', '.join(EVIDENCE_TYPES)}) and a ref")
            break
    texts = [st, draft.get("detail") or ""]
    for ev in draft.get("evidence") or []:
        texts += [str(ev.get("ref") or ""), str(ev.get("note") or "")]
    if any(contains_secret(t, secret_values) for t in texts):
        errs.append(_REFUSAL)
    return errs


def validate_episode_body(body: str, *, secret_values) -> list[str]:
    return [_REFUSAL] if contains_secret(body, secret_values) else []
