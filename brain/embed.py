"""Optional query and fact embeddings for vector search.

The brain works without this: `ask` uses full-text and commit lookup. Vector search is opt-in:
set "embeddings": true in .brain/config.json, install the `fastembed` package, and run
`brain embed`, which may download the 384-dimension model once. Queries never download: they load
the model from the local cache or go without. Nothing here is imported at start-up."""
from __future__ import annotations

import os

MODEL = os.getenv("BRAIN_EMBED_MODEL", "BAAI/bge-small-en")
_model = None


def _load(download=False):
    global _model
    if _model is None:
        if not download:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from fastembed import TextEmbedding
        # 4 threads by default: fastembed otherwise takes every core of a machine that is doing other work.
        _model = TextEmbedding(model_name=MODEL, threads=int(os.getenv("BRAIN_EMBED_THREADS", "4")))
    return _model


def embed_texts(texts, download=False):
    """A list of float lists, or None when no embedding model is available."""
    try:
        return [[float(x) for x in v] for v in _load(download).embed(list(texts))]
    except Exception:
        return None


def embed_query(q):
    out = embed_texts([q])
    return out[0] if out else None
