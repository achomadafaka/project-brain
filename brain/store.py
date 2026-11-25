"""Storage for the brain graph. RestStore speaks PostgREST over HTTP (standard library only).

Errors, by what the caller should do next:
  BrainUnavailable    the brain could not answer (transport, timeout, HTTP 402/408/429/5xx). Reads
                      fall back to the snapshot and writes go to the outbox.
  BrainMisconfigured  (a BrainUnavailable) the service key is not loaded. The schema denies anon,
                      and PostgREST answers an anon SELECT with [] and no error, so an anon client
                      would read an EMPTY brain. RestStore refuses to start instead.
  VersionConflict     the row moved since it was read: re-read and retry.
  BrainRefused        the database refused the request (a CHECK, a trigger, a missing row). Never
                      queued, because retrying a refused write cannot make it valid.

Section and entity reads go through database functions. A section's entity list can hold
thousands of ids and does not fit a URL, and the candidate stage needs EVERY fact on an entity,
oldest first, not a newest-first page."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from brain import READ_STATUSES


class BrainUnavailable(RuntimeError):
    pass


class BrainMisconfigured(BrainUnavailable):
    pass


class VersionConflict(RuntimeError):
    pass


class BrainRefused(RuntimeError):
    pass


_HTTP = re.compile(r"^HTTP (\d{3}) ")


def _q(v) -> str:
    return urllib.parse.quote(str(v), safe="")


def pg_array(values) -> str:
    """A Postgres array literal with every element quoted, so , { } " and spaces survive."""
    return "{" + ",".join('"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
                          for v in values) + "}"


def _pg_in(values) -> str:
    return "(" + ",".join('"' + str(v).replace('"', '\\"') + '"' for v in values) + ")"


FACT_COLUMNS = ("id,kind,statement,detail,entity_ids,polarity,status,judge_pending,valid_from,valid_to,"
                "recorded_at,expired_at,verify_by,evidence,author,author_session,verified_by,verified_at,"
                "verify_note,episode_ids,version")
ENTITY_COLUMNS = "id,type,name,name_lc,aliases,summary,section_id,created_at,updated_at"
EPISODE_COLUMNS = "id,kind,body,parent_id,part_no,author,session_id,occurred_at,recorded_at,source_ref"
# Reads never fetch the embedding or the search vector: nothing prints them and they are most of a row.
_READ_COLUMNS = {"brain_facts": FACT_COLUMNS, "brain_entities": ENTITY_COLUMNS,
                 "brain_episodes": EPISODE_COLUMNS}
_LIVE = f"&status=in.({','.join(READ_STATUSES)})&valid_to=is.null"


def http_request(base_url, key):
    """A request(method, path, body, headers, timeout) callable. Failures raise RuntimeError; an
    HTTP error's message starts "HTTP <code> " so the store can tell a refusal from an outage."""
    def request(method, path, body=None, headers=None, timeout=3.0):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{base_url}/{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} {method} {path.split('?')[0]}: "
                               f"{e.read().decode(errors='replace')[:300]}") from None
    return request


class RestStore:
    def __init__(self, config=None, request=None, timeout: float = 3.0):
        if request is None:
            key = config.service_key()
            if not key or not (config.rest_port or config.external_url):
                raise BrainMisconfigured("no service key or address for this brain; run `brain init`")
            request = http_request(config.url, key)
        self.config = config
        self._request = request
        self._timeout = timeout

    def _call(self, method, path, body=None, prefer=None):
        headers = {"Prefer": prefer} if prefer else None
        try:
            return self._request(method, path, body, headers, timeout=self._timeout)
        except RuntimeError as exc:
            msg = str(exc)
            m = _HTTP.match(msg)
            if not m:
                raise BrainUnavailable(msg[:200]) from None
            code = int(m.group(1))
            if code in (402, 408, 429) or code >= 500:
                raise BrainUnavailable(msg[:200]) from None
            if "version_conflict" in msg:
                raise VersionConflict(msg[:200]) from None
            raise BrainRefused(msg[:300]) from None
        except Exception as exc:
            raise BrainUnavailable(type(exc).__name__) from None

    def _rpc(self, name, args):
        return self._call("POST", f"rpc/{name}", args)

    def insert(self, table, row):
        return self._call("POST", f"{table}?select={_READ_COLUMNS.get(table, '*')}", row,
                          prefer="return=representation")[0]

    def get(self, table, id):
        rows = self._call("GET", f"{table}?select={_READ_COLUMNS.get(table, '*')}"
                                 f"&id=eq.{_q(id)}&limit=1") or []
        return rows[0] if rows else None

    def resolve_fact_id(self, prefix):
        """A full fact id from the 8+ hex-char prefix the boot prints. Ambiguous or unknown -> None."""
        p = str(prefix).strip().lower()
        if len(p) == 36:
            return p
        if not re.fullmatch(r"[0-9a-f-]{8,35}", p):
            return None
        hexs = p.replace("-", "")
        lo = (hexs + "0" * 32)[:32]
        hi = (hexs + "f" * 32)[:32]
        fmt = lambda h: f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        rows = self._call("GET", f"brain_facts?select=id&id=gte.{fmt(lo)}&id=lte.{fmt(hi)}&limit=2") or []
        return rows[0]["id"] if len(rows) == 1 else None

    def find_entity(self, type, name):
        """Exact name (case-insensitive through the generated name_lc column) or exact alias.
        Never a pattern: ilike treats `_` as a wildcard, and most setting names have one."""
        lc = name.strip().lower()
        rows = self._call("GET", f"brain_entities?select={ENTITY_COLUMNS}"
                                 f"&type=eq.{_q(type)}&name_lc=eq.{_q(lc)}&limit=1") or []
        if rows:
            return rows[0]
        rows = self._call("GET", f"brain_entities?select={ENTITY_COLUMNS}"
                                 f"&type=eq.{_q(type)}&aliases=cs.{_q(pg_array([lc]))}&limit=1") or []
        return rows[0] if rows else None

    def find_facts(self, entity_ids, kinds=None, live_only=True):
        """Every fact on any of the entities, OLDEST first, no cap."""
        if not entity_ids:
            return []
        args = {"p_entity_ids": list(entity_ids),
                "p_kinds": list(kinds) if kinds else None,
                "p_live_only": bool(live_only)}
        facts, after = [], None
        while True:
            path = f"rpc/brain_facts_for_entities?select={FACT_COLUMNS}&order=id.asc&limit=500"
            if after is not None:
                path += f"&id=gt.{_q(after)}"
            rows = self._call("POST", path, args) or []
            # A server cap can be smaller than our page; only empty means done.
            if not rows:
                return sorted(facts, key=lambda f: (f.get("recorded_at") or "", f["id"]))
            if after is not None and rows[-1]["id"] <= after:
                raise BrainRefused("paging did not advance")
            facts.extend(rows)
            after = rows[-1]["id"]

    def patch_fact(self, id, version, patch):
        rows = self._call("PATCH", f"brain_facts?id=eq.{_q(id)}&version=eq.{int(version)}&select={FACT_COLUMNS}",
                          patch, prefer="return=representation") or []
        if rows:
            return rows[0]
        if self.get("brain_facts", id) is None:
            raise BrainRefused(f"no fact {id}")
        raise VersionConflict(f"fact {id} is not at version {version}")

    def write_fact(self, row, actions):
        return self._call("POST", f"rpc/brain_write_fact?select={FACT_COLUMNS}",
                          {"p_new": row, "p_actions": actions})[0]

    def judge_pair(self, newer, older, verdict, author):
        return self._rpc("brain_judge_pair", {
            "p_newer": newer["id"], "p_older": older["id"], "p_newer_version": newer["version"],
            "p_older_version": older["version"], "p_verdict": verdict, "p_author": author})

    def search_facts(self, q, limit=20, any_word=False):
        """Live facts matching the query, best first. Every word must match; with any_word, at
        least two distinct words must."""
        # Split on anything that is not a letter or digit and KEEP the parts: Postgres' English
        # parser splits "MAX_RETRY_COUNT" or "scripts/tool.py" the same way, so the parts match.
        words = " ".join(w for w in re.split(r"[^A-Za-z0-9]+", q) if w)
        if not words:
            return []
        return self._call("POST", f"rpc/brain_search_facts?select={FACT_COLUMNS}",
                          {"p_query": words, "p_any": bool(any_word), "p_limit": int(limit)}) or []

    def match_facts(self, embedding, limit=20):
        """Nearest live facts to a query embedding (a list of floats), or [] without one."""
        if embedding is None:
            return []
        vec = "[" + ",".join(f"{float(x):.7g}" for x in embedding) + "]"
        hits = self._rpc("brain_match_facts", {"query_embedding": vec, "match_count": int(limit)}) or []
        if not hits:
            return []
        rows = {r["id"]: r for r in (self._call(
            "GET", f"brain_facts?select={FACT_COLUMNS}&id=in." + _q(_pg_in(h["id"] for h in hits)) + _LIVE) or [])}
        return [rows[h["id"]] for h in hits if h["id"] in rows]

    def facts_missing_embedding(self, limit=64):
        return self._call("GET", f"brain_facts?select=id,statement,detail&embedding=is.null{_LIVE}"
                                 f"&order=id.asc&limit={int(limit)}") or []

    def set_embeddings(self, rows):
        """rows: [{"id": ..., "embedding": [floats]}], at most 64. Returns how many were written."""
        return int(self._rpc("brain_set_embeddings", {"p_rows": [
            {"table": "brain_facts", "id": r["id"], "embedding": "[" + ",".join(f"{float(x):.7g}" for x in r["embedding"]) + "]"}
            for r in rows]}) or 0)

    def facts_by_sha(self, token, limit=20):
        """Live facts that name a commit: evidence {type: sha} whose ref equals the token or one of
        its 7-12 char prefixes (stored refs are 7-40 chars), plus facts on a commit entity whose
        name starts with the token. A token SHORTER than a stored evidence ref only resolves
        through the entity."""
        refs = [token] + [token[:n] for n in (7, 8, 9, 10, 12) if n < len(token)]
        seen, out = set(), []
        live = f"{_LIVE}&limit={int(limit)}"
        for ref in dict.fromkeys(refs):
            for f in self._call("GET", f"brain_facts?select={FACT_COLUMNS}&evidence=cs."
                                       f"{_q(json.dumps([{'type': 'sha', 'ref': ref}]))}{live}") or []:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    out.append(f)
        ents = self._call("GET", f"brain_entities?select=id&type=eq.commit&name_lc=like.{_q(token.lower() + '*')}&limit=5") or []
        more = (self._call("POST", f"rpc/brain_facts_for_entities?select={FACT_COLUMNS}&order=id.asc&limit={int(limit)}",
                           {"p_entity_ids": [e["id"] for e in ents], "p_kinds": None, "p_live_only": True}) or []) if ents else []
        for f in more:
            if f["id"] not in seen:
                seen.add(f["id"])
                out.append(f)
        return out[:limit]

    def facts_in_sections(self, section_ids, kinds=None, *, queue_only=False, exclude_author=None,
                          oldest_first=False, limit=500):
        if not section_ids:
            return []
        return self._call("POST", "rpc/brain_facts_in_sections?select=" + FACT_COLUMNS, {
            "p_section_ids": list(section_ids), "p_kinds": list(kinds) if kinds else None,
            "p_queue_only": bool(queue_only), "p_exclude_author": exclude_author,
            "p_oldest_first": bool(oldest_first), "p_limit": int(limit)}) or []

    def queue_count(self, section_ids, exclude_author=None) -> int:
        if not section_ids:
            return 0
        n = self._rpc("brain_queue_count", {"p_section_ids": list(section_ids),
                                            "p_exclude_author": exclude_author})
        return int(n if isinstance(n, int) else (n[0] if n else 0))

    def unverified_queue(self, role, section_ids, limit):
        """Unverified or judge_pending facts another role wrote, OLDEST first (the true oldest,
        chosen in the database, not the oldest of a newest-first page)."""
        return self.facts_in_sections(section_ids, None, queue_only=True, exclude_author=role,
                                      oldest_first=True, limit=limit)

    def sections(self, slugs=None):
        path = "brain_sections?order=slug.asc"
        if slugs:
            path += "&slug=in." + _q(_pg_in(slugs))
        return self._call("GET", path) or []

    def ensure_sections(self, slugs):
        """Create the sections that do not exist yet. Returns every section."""
        have = {s["slug"] for s in self.sections()}
        for slug in slugs:
            if slug not in have:
                self.insert("brain_sections", {"slug": slug, "title": slug})
        return self.sections()

    def episodes_since(self, kinds, since_iso, limit):
        """Newest-first episodes of the given kinds recorded at/after since_iso (the boot's 48 h)."""
        return self._call("GET", f"brain_episodes?select={EPISODE_COLUMNS}&kind=in."
                                 + _q(_pg_in(kinds))
                                 + f"&recorded_at=gte.{_q(since_iso)}&order=recorded_at.desc"
                                 f"&limit={int(limit)}") or []
