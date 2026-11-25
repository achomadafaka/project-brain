"""In-memory store that carries the schema's CHECKs and triggers, for offline tests.

Every rule the database enforces is mirrored here and raises BrainRefused, the error RestStore
raises for a database refusal. A rule that exists only in the fake would let a test pass against
behaviour the real database refuses, so keep the two in step with brain/schema.sql. Role names and
entity types are checked here the way the client checks them, from the project config."""
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from brain import EPISODE_KINDS, KINDS, READ_STATUSES, STATUSES, VERDICTS
from conftest import make_config
from brain.store import BrainRefused, VersionConflict

_DAYS = {"measurement": 7, "settled": 60, "task": 3}
_VERIFIED = ("verified", "verified_partial", "refuted")
_EP_KINDS = EPISODE_KINDS
_RELS = ("supports", "contradicts", "supersedes", "depends_on", "derived_from", "same_as", "part_of",
         "candidate")
_FACT_IMMUTABLE = ("statement", "kind", "author", "recorded_at", "entity_ids", "valid_from", "polarity",
                   "author_session")


def _refs(evidence):
    return sorted((str(e.get("type")), str(e.get("ref"))) for e in (evidence or []))


class FakeStore:
    def __init__(self, config=None):
        self.config = config or make_config()
        self.rows = {t: [] for t in ("brain_sections", "brain_episodes", "brain_entities", "brain_facts",
                                     "brain_links", "brain_judgements")}
        self.clock = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    def _now(self):
        self.clock += timedelta(seconds=1)
        return self.clock.isoformat()

    # ------------------------------------------------------------------ inserts
    def insert(self, table, row):
        row = dict(row)
        row.setdefault("id", str(uuid.uuid4()))
        getattr(self, "_check_" + table[len("brain_"):], lambda r: None)(row)
        self.rows[table].append(row)
        return dict(row)

    def _check_sections(self, row):
        if any(s["slug"] == row["slug"] for s in self.rows["brain_sections"]):
            raise BrainRefused("duplicate section slug")

    def _check_entities(self, row):
        if row.get("type") not in self.config.entity_types:
            raise BrainRefused(f"entity type {row.get('type')!r} violates the type CHECK")
        name = (row.get("name") or "").strip()
        if not 1 <= len(name) <= 300:
            raise BrainRefused("entity name length")
        if not row.get("section_id") or not any(s["id"] == row["section_id"] for s in self.rows["brain_sections"]):
            raise BrainRefused("section_id is NOT NULL and must reference a section")
        lc = row["name"].lower()
        aliases = sorted({a.strip().lower() for a in row.get("aliases") or [] if a.strip()} - {lc})
        for e in self.rows["brain_entities"]:
            if e["type"] != row["type"]:
                continue
            if e["name_lc"] == lc:
                raise BrainRefused("duplicate (type, name_lc)")
            if e["name_lc"] in aliases or set(e["aliases"]) & (set(aliases) | {lc}):
                raise BrainRefused(f"alias_conflict with entity {e['id']}")
        row.update(name_lc=lc, aliases=aliases)
        row.setdefault("summary", None)

    def _check_facts(self, row):
        if row.get("kind") not in KINDS:
            raise BrainRefused("kind CHECK")
        if not 8 <= len(row.get("statement") or "") <= 600:
            raise BrainRefused("statement length CHECK")
        if len(row.get("detail") or "") > 4096:
            raise BrainRefused("detail length CHECK")
        if not row.get("entity_ids"):
            raise BrainRefused("entity_ids cardinality CHECK")
        if row.get("author") not in self.config.authors:
            raise BrainRefused("author CHECK")
        if row["kind"] != "observation" and not row.get("evidence"):
            raise BrainRefused("evidence CHECK")
        if row["kind"] == "observation" and not row.get("episode_ids"):
            raise BrainRefused("observation episode CHECK")
        now = self._now()
        row.update(recorded_at=now, version=1)
        row.setdefault("status", "unverified")
        row.setdefault("judge_pending", False)
        row.setdefault("valid_from", now)
        row.setdefault("valid_to", None)
        row.setdefault("expired_at", None)
        row.setdefault("episode_ids", [])
        row.setdefault("polarity", 0)
        row.setdefault("detail", None)
        row.setdefault("author_session", None)
        row.setdefault("verified_by", None)
        row.setdefault("verified_at", None)
        row.setdefault("verify_note", None)
        if row["status"] not in STATUSES:
            raise BrainRefused("status CHECK")
        if row.get("verify_by") is None:
            row["verify_by"] = ("infinity" if row["kind"] == "observation" else
                                (self.clock + timedelta(days=_DAYS.get(row["kind"], 30))).isoformat())
        self._check_fact_row(row)

    def _check_fact_row(self, r):
        if r["verified_by"] is not None and r["verified_by"] not in self.config.verifiers:
            raise BrainRefused("verified_by CHECK")
        if r["verified_by"] is not None and r["verified_by"] == r["author"]:
            raise BrainRefused("CHECK verified_by <> author")
        if r["status"] in _VERIFIED and r["verified_by"] is None:
            raise BrainRefused("CHECK verified status needs verified_by")
        if (r["valid_to"] is not None and datetime.fromisoformat(r["valid_to"].replace("Z", "+00:00"))
                < datetime.fromisoformat(r["valid_from"].replace("Z", "+00:00"))):
            raise BrainRefused("CHECK valid_to >= valid_from")

    def _check_episodes(self, row):
        if row.get("kind") not in _EP_KINDS:
            raise BrainRefused("episode kind CHECK")
        if len(row.get("body") or "") > 1800:
            raise BrainRefused("episode part over 1800 chars")
        if row.get("author") not in self.config.authors:
            raise BrainRefused("episode author CHECK")
        row.setdefault("part_no", 1)
        row.setdefault("parent_id", None)
        row.setdefault("occurred_at", None)                  # NULL = unknown, never "now"
        row["recorded_at"] = self._now()

    def _check_links(self, row):
        if row.get("rel") not in _RELS or row.get("author") not in self.config.authors:
            raise BrainRefused("link CHECK")
        if any((l["from_id"], l["to_id"], l["rel"]) == (row["from_id"], row["to_id"], row["rel"])
               for l in self.rows["brain_links"]):
            raise BrainRefused("duplicate link")

    # ------------------------------------------------------------------ fact updates (the trigger)
    def patch_fact(self, id, version, patch):
        for r in self.rows["brain_facts"]:
            if r["id"] != id:
                continue
            if r["version"] != version:
                raise VersionConflict(id)
            new = dict(r, **patch)
            for k in _FACT_IMMUTABLE:
                if new.get(k) != r.get(k):
                    raise BrainRefused(f"{k} is immutable; supersede instead")
            for k in ("valid_to", "expired_at"):
                if r[k] is not None and new[k] != r[k]:
                    raise BrainRefused("valid_to is write-once (a closed fact cannot be revived)")
            if _refs(new.get("evidence")) != _refs(r.get("evidence")):
                if new["status"] in _VERIFIED:
                    new["status"] = "unverified"
                new["verified_by"] = None
                new["verified_at"] = None
            if new["status"] in _VERIFIED and new["status"] != r["status"]:
                if r["status"] == "disputed":
                    raise BrainRefused("resolve the dispute first")
                if new["judge_pending"]:
                    raise BrainRefused("judge_pending candidates must be judged before verification")
            if new["status"] not in STATUSES:
                raise BrainRefused("status CHECK")
            self._check_fact_row(new)
            new["version"] = r["version"] + 1
            r.clear()
            r.update(new)
            return dict(r)
        raise BrainRefused(f"no fact {id}")

    def write_fact(self, row, actions):
        """Mirror brain_write_fact's single transaction, including rollback on a rejected graph write."""
        existing = self.get("brain_facts", row["id"])
        if existing:
            if any(existing[k] != row[k] for k in ("kind", "statement", "author", "entity_ids")):
                raise BrainRefused("operation_identity_conflict")
            return existing
        before = {table: deepcopy(self.rows.get(table, []))
                  for table in ('brain_facts', 'brain_links', 'brain_judgements')}
        try:
            for action in actions:
                if action["verdict"] in ("supersedes", "contradicts"):
                    old = self.get("brain_facts", action["id"])
                    if (not old or old["version"] != action["version"] or not self._live(old)):
                        raise VersionConflict("version_conflict")
            starts = row.get("valid_from") or self._now()
            for action in actions:
                if action["verdict"] == "supersedes":
                    old = self.get("brain_facts", action["id"])
                    if starts < old["valid_from"]:
                        raise BrainRefused("supersede_backdated")
            new = self.insert("brain_facts", dict(row, valid_from=starts,
                status="disputed" if any(a["verdict"] == "contradicts" for a in actions) else "unverified",
                judge_pending=any(a["verdict"] == "candidate" for a in actions)))
            for action in actions:
                verdict = action["verdict"]
                if verdict in ("supersedes", "contradicts"):
                    patch = ({"status": "superseded", "valid_to": starts, "expired_at": new["recorded_at"]}
                             if verdict == "supersedes" else {"status": "disputed"})
                    self.patch_fact(action["id"], action["version"], patch)
                if verdict != "candidate":
                    self.record_judgement(new["id"], action["id"], verdict, new["author"])
                if verdict != "unrelated":
                    self.insert("brain_links", {"from_id": new["id"], "from_kind": "fact",
                        "to_id": action["id"], "to_kind": "fact",
                        "rel": "derived_from" if verdict == "refines" else verdict, "author": new["author"]})
            return new
        except BaseException:
            self.rows.update(before)
            raise

    def judge_pair(self, newer, older, verdict, author):
        """Mirrors brain_judge_pair."""
        n_id, o_id = newer["id"], older["id"]
        if n_id == o_id:
            raise BrainRefused("same_fact")
        if verdict not in VERDICTS:
            raise BrainRefused("invalid_verdict")
        n, o = self.get("brain_facts", n_id), self.get("brain_facts", o_id)
        rel = {"duplicate": "same_as", "refines": "derived_from", "supersedes": "supersedes",
               "contradicts": "contradicts"}.get(verdict)
        lo, hi = sorted((n_id, o_id))
        prior = next((j for j in self.rows["brain_judgements"] if (j["pair_lo"], j["pair_hi"]) == (lo, hi)), None)
        linked = lambda: any((l["from_id"], l["to_id"], l["rel"]) == (n_id, o_id, rel) for l in self.rows["brain_links"])
        if prior:
            if prior["verdict"] != verdict:
                raise BrainRefused("judgement_conflict")
            if verdict in ("duplicate", "refines", "supersedes") and not linked():
                raise BrainRefused("judgement_orientation_conflict")
            return {"verdict": verdict, "replayed": True}
        if n["version"] != newer["version"] or o["version"] != older["version"]:
            raise VersionConflict("version_conflict")
        if verdict != "unrelated" and (not self._live(n) or (not self._live(o) and verdict != "refines")):
            raise BrainRefused("closed_fact")
        before = deepcopy(self.rows)
        try:
            now = self._now()
            if verdict == "duplicate":
                self.patch_fact(n_id, n["version"], {"status": "retracted", "valid_to": now, "expired_at": now})
            elif verdict == "supersedes":
                if n["valid_from"] < o["valid_from"]:
                    raise BrainRefused("backdated_supersession")
                self.patch_fact(o_id, o["version"], {"status": "superseded", "valid_to": n["valid_from"], "expired_at": now})
            elif verdict == "contradicts":
                for f in (n, o):
                    self.patch_fact(f["id"], f["version"], {"status": "disputed"})
            if rel and not linked():
                self.insert("brain_links", {"from_id": n_id, "from_kind": "fact", "to_id": o_id, "to_kind": "fact",
                                            "rel": rel, "author": author})
            self.record_judgement(n_id, o_id, verdict, author)
            judged = {(j["pair_lo"], j["pair_hi"]) for j in self.rows["brain_judgements"]}
            for fid in (n_id, o_id):
                f = self.get("brain_facts", fid)
                open_links = [l for l in self.rows["brain_links"] if l["rel"] == "candidate" and fid in (l["from_id"], l["to_id"])
                              and tuple(sorted((l["from_id"], l["to_id"]))) not in judged]
                if f["judge_pending"] and not open_links:
                    self.patch_fact(fid, f["version"], {"judge_pending": False})
            return {"verdict": verdict, "replayed": False}
        except BaseException:
            self.rows = before
            raise

    # ------------------------------------------------------------------ reads
    def resolve_fact_id(self, prefix):
        p = str(prefix).strip().lower()
        hits = [r["id"] for r in self.rows["brain_facts"] if r["id"].lower().startswith(p)]
        return hits[0] if len(hits) == 1 else None

    def get(self, table, id):
        return next((dict(r) for r in self.rows[table] if r["id"] == id), None)

    def find_entity(self, type, name):
        lc = name.strip().lower()
        return next((dict(r) for r in self.rows["brain_entities"] if r["type"] == type and
                     (r["name_lc"] == lc or lc in r["aliases"])), None)

    def _live(self, f):
        return f["status"] in READ_STATUSES and f["valid_to"] is None

    def find_facts(self, entity_ids, kinds=None, live_only=True):
        out = [f for f in self.rows["brain_facts"]
               if set(f["entity_ids"]) & set(entity_ids) and (not kinds or f["kind"] in kinds)
               and (not live_only or self._live(f))]
        return [dict(f) for f in sorted(out, key=lambda f: f["recorded_at"])]

    def search_facts(self, q, limit=20, any_word=False):
        import re
        words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", q) if w]
        hits = [f for f in self.rows["brain_facts"] if self._live(f)
                and any(w in (f["statement"] + " " + (f.get("detail") or "")).lower() for w in words)]
        return [dict(f) for f in hits[:limit]]

    def match_facts(self, embedding, limit=20):
        return []

    def facts_by_sha(self, token, limit=20):
        refs = {token} | {token[:n] for n in (7, 8, 9, 10, 12) if n < len(token)}
        ents = {e["id"] for e in self.rows["brain_entities"] if e["type"] == "commit" and e["name_lc"].startswith(token)}
        out = [f for f in self.rows["brain_facts"] if self._live(f) and (
            any(e.get("type") == "sha" and e.get("ref") in refs for e in f.get("evidence") or []) or set(f["entity_ids"]) & ents)]
        return [dict(f) for f in out[:limit]]

    def _in_sections(self, section_ids):
        ents = {e["id"] for e in self.rows["brain_entities"] if e["section_id"] in section_ids}
        return [f for f in self.rows["brain_facts"] if self._live(f) and f["kind"] != "observation"
                and set(f["entity_ids"]) & ents]

    def facts_in_sections(self, section_ids, kinds=None, *, queue_only=False, exclude_author=None,
                          oldest_first=False, limit=500):
        out = [f for f in self._in_sections(section_ids) if (not kinds or f["kind"] in kinds)
               and (not queue_only or f["status"] == "unverified" or f["judge_pending"])
               and (exclude_author is None or f["author"] != exclude_author)]
        out.sort(key=lambda f: f["recorded_at"], reverse=not oldest_first)
        return [dict(f) for f in out[:max(1, limit)]]

    def queue_count(self, section_ids, exclude_author=None):
        return len([f for f in self._in_sections(section_ids)
                    if (f["status"] == "unverified" or f["judge_pending"])
                    and (exclude_author is None or f["author"] != exclude_author)])

    def unverified_queue(self, role, section_ids, limit):
        return self.facts_in_sections(section_ids, None, queue_only=True, exclude_author=role,
                                      oldest_first=True, limit=limit)

    def sections(self, slugs=None):
        return [dict(s) for s in sorted(self.rows["brain_sections"], key=lambda s: s["slug"])
                if not slugs or s["slug"] in slugs]

    def ensure_sections(self, slugs):
        for slug in slugs:
            if not self.sections([slug]):
                self.insert("brain_sections", {"slug": slug, "title": slug})
        return self.sections()

    def episodes_since(self, kinds, since_iso, limit):
        eps = [e for e in self.rows["brain_episodes"]
               if e["kind"] in kinds
               and (e.get("recorded_at") or "") >= since_iso]
        return [dict(e) for e in sorted(eps, key=lambda e: e.get("recorded_at") or "", reverse=True)][:limit]

    def record_judgement(self, a, b, verdict, author):
        if a == b or verdict not in VERDICTS or not author:
            raise BrainRefused('judgement CHECK')
        lo, hi = sorted((a, b))
        rows = self.rows['brain_judgements']
        if not any((j['pair_lo'], j['pair_hi']) == (lo, hi) for j in rows):
            rows.append(dict(pair_lo=lo, pair_hi=hi, verdict=verdict, author=author, created_at=self._now()))
