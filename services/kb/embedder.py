"""Local embedding engine for the knowledgebase (optional, CPU-only).

fastembed (ONNX Runtime — no torch) computes BAAI/bge-small-en-v1.5 vectors
(384-dim) locally, so semantic search adds zero API cost. Everything degrades
gracefully: with KB_EMBEDDINGS=off, or if fastembed/numpy aren't installed,
enabled() is False, embed_query() returns None and retrieval falls back to
keyword-only FULLTEXT search.

Vectors are L2-normalized float32 and stored packed (ndarray.tobytes()) in
kb_chunks.embedding, so cosine similarity == plain dot product after unpack().

The model cache lives in KB_EMBED_CACHE (defaults under /store/droptracker —
NOT under $HOME, so systemd services with restricted home dirs still work).
"""

import os
import threading

try:
    import numpy as np
except Exception:  # numpy arrives alongside fastembed; keyword-only until then
    np = None

MODEL_NAME = os.getenv("KB_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
CACHE_DIR = os.getenv("KB_EMBED_CACHE", "/store/droptracker/disc/.fastembed_cache")

_model = None
_model_lock = threading.Lock()
_fastembed_ok = None  # memoized import probe: None=unknown, True/False=known


def _env_on() -> bool:
    return os.getenv("KB_EMBEDDINGS", "on").strip().lower() not in ("off", "0", "false", "no")


def enabled() -> bool:
    """True when embeddings are switched on AND the local stack is importable."""
    global _fastembed_ok
    if not _env_on() or np is None:
        return False
    if _fastembed_ok is None:
        try:
            import fastembed  # noqa: F401
            _fastembed_ok = True
        except Exception:
            _fastembed_ok = False
    return _fastembed_ok


def _get_model():
    """Lazy singleton — model load (and first-run download) happens on first use,
    never at import time, so bot startup is never blocked."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from fastembed import TextEmbedding

                os.makedirs(CACHE_DIR, exist_ok=True)
                _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=CACHE_DIR)
    return _model


def _normalize(vec) -> "np.ndarray":
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def embed_passages(texts) -> list:
    """Embed chunk texts for storage.

    Returns a list of packed little-endian float32 bytes, aligned with the
    input order. Raises if the stack is unavailable — callers gate on
    enabled() first (the miner skips embedding entirely when disabled).
    """
    texts = list(texts)
    if not texts:
        return []
    model = _get_model()
    return [_normalize(v).tobytes() for v in model.passage_embed(texts)]


def embed_query(text: str):
    """Embed a search query (bge query-side instruction applied by fastembed).

    Returns a normalized float32 ndarray, or None when embeddings are
    unavailable — the retriever treats None as "keyword-only mode".
    """
    if not enabled():
        return None
    try:
        model = _get_model()
        for v in model.query_embed(text):
            return _normalize(v)
    except Exception:
        return None
    return None


def unpack(blob: bytes):
    """kb_chunks.embedding bytes -> float32 ndarray (read-only view)."""
    return np.frombuffer(blob, dtype=np.float32)
