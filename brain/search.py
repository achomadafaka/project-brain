"""Hybrid retrieval: commit SHA + full-text + (optional) vector, fused by reciprocal rank, then a
one-hop expansion to the newest facts sharing an entity with the top hit."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from brain.embed import embed_query


# Age = valid_from (the event date), else recorded_at.
# Vector search returns the same short generic rules at high similarity for ANY query, so a vector-only hit is a
# tie-breaker, not evidence: it must rank below every keyword hit. Newer facts get up to +30% so a decision made
# today is not buried one keyword rank behind an old finding; unknown age gets no boost.
W_KEYWORD, W_VECTOR = 1.0, 0.25
RECENCY_BOOST, RECENCY_HALF_LIFE_DAYS = 0.3, 30.0
VECTOR_ONLY_CAP = 5                            # a hit found ONLY by vector search: the 30 nearest are generic whatever the query, so show 5
W_SHA = 4.0                                    # a commit named in the query is the strongest key there is
_SHA = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,40}(?![0-9A-Za-z])")


def _shas(q):
    """Commit-like tokens: 7-40 hex chars with at least one digit AND one letter, so 'defaced' and 1793353688 are not shas."""
    out = []
    for t in _SHA.findall(q):
        t = t.lower()
        if any(c.isdigit() for c in t) and any(c in "abcdef" for c in t) and t not in out:
            out.append(t)
    return out[:3]


def _recency(f, now):
    try:
        at = datetime.fromisoformat(str(f.get("valid_from") or f["recorded_at"]).replace("Z", "+00:00"))
        if at.tzinfo is None:
            return 0.0
        return 0.5 ** (max(0.0, (now - at).total_seconds()) / 86400.0 / RECENCY_HALF_LIFE_DAYS)
    except (KeyError, TypeError, ValueError):
        return 0.0


def ask(store, q, *, k=60, limit=15, embed=True, now=None, stats=None):
    now = now or datetime.now(timezone.utc)
    ranked, lexical = {}, set()
    sources = [(W_SHA, store.facts_by_sha(t, 20)) for t in _shas(q)]
    # Every word first; a question in prose rarely matches whole, so fall back to any two words.
    keyword = store.search_facts(q, 30) or store.search_facts(q, 30, any_word=True)
    # Vector search is opt-in per project ("embeddings": true in .brain/config.json).
    vector = store.match_facts(embed_query(q) if embed and getattr(store.config, "embeddings", False) else None, 30)
    for weight, hits in sources + [(W_KEYWORD, keyword), (W_VECTOR, vector)]:
        for rank, f in enumerate(hits):
            ranked.setdefault(f["id"], [f, 0.0])
            ranked[f["id"]][1] += weight / (k + rank + 1)
            if weight != W_VECTOR:
                lexical.add(f["id"])
    vector_only = sorted((i for i in ranked if i not in lexical), key=lambda i: -ranked[i][1])
    excluded = set(vector_only[VECTOR_ONLY_CAP:])
    for i in excluded:
        del ranked[i]
    if stats is not None:
        stats.update(vector_only_seen=len(vector_only), vector_only_cut=max(0, len(vector_only) - VECTOR_ONLY_CAP),
                     vector_only_cap=VECTOR_ONLY_CAP)
    for entry in ranked.values():
        entry[1] *= 1.0 + RECENCY_BOOST * _recency(entry[0], now)
    top = [f for f, _ in sorted(ranked.values(), key=lambda x: -x[1])]
    if top:
        seen = {f["id"] for f in top}
        near = store.find_facts(top[0]["entity_ids"])[::-1]          # find_facts is oldest first
        top += [f for f in near if f["id"] not in seen and f["id"] not in excluded][:5]
    return top[:limit]
