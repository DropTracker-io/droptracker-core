"""Hybrid retrieval for the DropTracker knowledgebase.

Two arms, fused with reciprocal rank fusion (RRF, k=60):

* keyword  — MariaDB InnoDB FULLTEXT ``MATCH ... AGAINST`` (natural language),
             always available.
* semantic — brute-force cosine over a locally-cached matrix of chunk
             embeddings, active only when the optional embedder stack
             (``services.kb.embedder``) can produce a query vector.

Everything degrades to keyword-only whenever embeddings are unavailable
(``embedder.embed_query`` returns ``None``). numpy is imported lazily and only
on the semantic path, so this module imports and runs with numpy absent.

Public API:
    search(query, top_k=8, source_types=None) -> list[dict]
    stats() -> dict
"""

import time

from sqlalchemy import text, bindparam

from db.models import KBDocument, KBChunk, Session
from services.kb import embedder

# How many ids each arm surfaces into the fusion step.
_KEYWORD_FETCH = 40
_SEMANTIC_FETCH = 40


# --------------------------------------------------------------------------- #
# Keyword arm (FULLTEXT)                                                       #
# --------------------------------------------------------------------------- #
def _keyword_ids(query: str, source_types: list[str] | None) -> list[int]:
    """Top FULLTEXT chunk ids in relevance order (best first).

    The query text is passed ONLY as a bound value — never interpolated.
    """
    sql = """
        SELECT c.id, MATCH(c.content) AGAINST (:q IN NATURAL LANGUAGE MODE) AS rel
        FROM kb_chunks c
        JOIN kb_documents d ON d.id = c.document_id
        WHERE MATCH(c.content) AGAINST (:q IN NATURAL LANGUAGE MODE) > 0
        {tf}
        ORDER BY rel DESC
        LIMIT :n
    """
    params = {"q": query, "n": _KEYWORD_FETCH}
    if source_types:
        stmt = text(sql.format(tf="AND d.source_type IN :types")).bindparams(
            bindparam("types", expanding=True)
        )
        params["types"] = list(source_types)
    else:
        stmt = text(sql.format(tf=""))
    with Session() as s:
        rows = s.execute(stmt, params).fetchall()
    return [int(r[0]) for r in rows]


# --------------------------------------------------------------------------- #
# Semantic arm (cosine over cached embeddings)                                 #
# --------------------------------------------------------------------------- #
_VEC_TTL = 60.0
# Module-level cache of the embedded-chunk matrix. ``types`` runs parallel to
# ``ids``/``mat`` so the source_type filter can be applied in-memory without a
# second round-trip.
_vec_cache: dict = {
    "ids": None,
    "mat": None,
    "types": None,
    "count": -1,
    "loaded_at": 0.0,
}


def _embedded_count() -> int:
    with Session() as s:
        return int(
            s.execute(
                text("SELECT COUNT(*) FROM kb_chunks WHERE embedding IS NOT NULL")
            ).scalar()
            or 0
        )


def _refresh_vec_cache(np) -> None:
    """(Re)load id / embedding / source_type for every embedded chunk."""
    with Session() as s:
        rows = s.execute(
            text(
                "SELECT c.id, c.embedding, d.source_type "
                "FROM kb_chunks c "
                "JOIN kb_documents d ON d.id = c.document_id "
                "WHERE c.embedding IS NOT NULL"
            )
        ).fetchall()
    if not rows:
        _vec_cache.update(
            ids=np.empty((0,), dtype=np.int64),
            mat=None,
            types=[],
            count=0,
            loaded_at=time.time(),
        )
        return
    ids = np.array([int(r[0]) for r in rows], dtype=np.int64)
    mat = np.vstack([embedder.unpack(r[1]) for r in rows]).astype(np.float32, copy=False)
    types = [r[2] for r in rows]
    _vec_cache.update(
        ids=ids, mat=mat, types=types, count=len(rows), loaded_at=time.time()
    )


def _semantic_ids(qvec, source_types: list[str] | None) -> list[int]:
    """Top cosine-similar chunk ids (best first).

    Reached only when ``embedder.embed_query`` returned a vector, which
    guarantees numpy is importable. Any source_type filter is applied AFTER
    ranking (the cached matrix carries no filter), dropping non-matching ids
    while preserving order. Returns [] when no embeddings exist.
    """
    import numpy as np  # guarded: only on the semantic path

    now = time.time()
    stale = _vec_cache["ids"] is None or (
        now - _vec_cache["loaded_at"] > _VEC_TTL
        and _embedded_count() != _vec_cache["count"]
    )
    if stale:
        _refresh_vec_cache(np)

    ids = _vec_cache["ids"]
    mat = _vec_cache["mat"]
    if mat is None or ids is None or len(ids) == 0:
        return []

    types = _vec_cache["types"]
    sims = mat @ np.asarray(qvec, dtype=np.float32)

    n = sims.shape[0]
    k = min(_SEMANTIC_FETCH, n)
    if k <= 0:
        return []
    # Cheap top-k selection, then order those k descending by similarity.
    part = np.argpartition(-sims, k - 1)[:k]
    order = part[np.argsort(-sims[part])]

    allowed = set(source_types) if source_types else None
    out: list[int] = []
    for i in order:
        if allowed is not None and types[i] not in allowed:
            continue
        out.append(int(ids[i]))
    return out


# --------------------------------------------------------------------------- #
# Fusion + hydration                                                           #
# --------------------------------------------------------------------------- #
def _rrf(rank_lists: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for rank, cid in enumerate(ranks):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda t: -t[1])


def _hydrate(ids: list[int], source_types: list[str] | None) -> dict[int, dict]:
    """Fetch chunk+document fields for ``ids`` (order-agnostic), keyed by id.

    Applies the source_type filter here too, so filtered-out chunks simply
    never make it into the returned map.
    """
    if not ids:
        return {}
    with Session() as s:
        q = (
            s.query(KBChunk, KBDocument)
            .join(KBDocument, KBDocument.id == KBChunk.document_id)
            .filter(KBChunk.id.in_(ids))
        )
        if source_types:
            q = q.filter(KBDocument.source_type.in_(source_types))
        pairs = q.all()
    out: dict[int, dict] = {}
    for chunk, doc in pairs:
        out[int(chunk.id)] = {
            "chunk_id": int(chunk.id),
            "document_id": int(chunk.document_id),
            "source_type": doc.source_type,
            "source_ref": doc.source_ref,
            "title": doc.title,
            "url": doc.url,
            "chunk_index": int(chunk.chunk_index),
            "content": chunk.content,
        }
    return out


def search(
    query: str, top_k: int = 8, source_types: list[str] | None = None
) -> list[dict]:
    """Hybrid keyword + semantic retrieval fused with RRF.

    Returns up to ``top_k`` dicts (fusion order, best first), each:
        chunk_id, document_id, source_type, source_ref, title, url,
        chunk_index, content, score, keyword_rank, semantic_rank
    where the per-arm ranks are 1-based ints, or None if that arm did not
    surface the chunk.
    """
    query = (query or "").strip()
    if not query:
        return []

    kw_ids = _keyword_ids(query, source_types)

    sem_ids: list[int] = []
    qvec = embedder.embed_query(query)
    if qvec is not None:
        sem_ids = _semantic_ids(qvec, source_types)

    fused = _rrf([kw_ids, sem_ids])
    if not fused:
        return []

    kw_rank = {cid: i + 1 for i, cid in enumerate(kw_ids)}
    sem_rank = {cid: i + 1 for i, cid in enumerate(sem_ids)}
    score_by_id = dict(fused)
    fused_ids = [cid for cid, _ in fused]

    hydrated = _hydrate(fused_ids, source_types)

    out: list[dict] = []
    for cid in fused_ids:
        row = hydrated.get(cid)
        if row is None:
            continue  # dropped by the source_type filter
        row = dict(row)
        row["score"] = float(score_by_id[cid])
        row["keyword_rank"] = kw_rank.get(cid)
        row["semantic_rank"] = sem_rank.get(cid)
        out.append(row)
        if len(out) >= top_k:
            break
    return out


# --------------------------------------------------------------------------- #
# Admin read utility                                                           #
# --------------------------------------------------------------------------- #
def stats() -> dict:
    """Corpus counts for the admin bot's /kb-stats."""
    with Session() as s:
        by_type = s.execute(
            text("SELECT source_type, COUNT(*) FROM kb_documents GROUP BY source_type")
        ).fetchall()
        chunks = int(s.execute(text("SELECT COUNT(*) FROM kb_chunks")).scalar() or 0)
        embedded = int(
            s.execute(
                text("SELECT COUNT(*) FROM kb_chunks WHERE embedding IS NOT NULL")
            ).scalar()
            or 0
        )
    return {
        "documents_by_type": {str(t): int(c) for t, c in by_type},
        "chunks": chunks,
        "chunks_embedded": embedded,
    }
