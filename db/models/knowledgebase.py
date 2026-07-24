from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    ForeignKey,
    Index,
    UniqueConstraint,
    LargeBinary,
    func,
)
from db.models.base import Base

# Knowledgebase tables (kb01a). Sources mined from Discord (ticket transcripts,
# suggestion/bug forums, general chat) plus repo docs are normalized into
# KBDocument (one row per ticket / forum thread / chat channel-month / doc file)
# and chunked into KBChunk rows for retrieval. Retrieval is hybrid:
#   - keyword: MATCH(content) AGAINST(...) via the FULLTEXT index below
#     (first FULLTEXT index in this schema; MariaDB 10.11 InnoDB supports it)
#   - semantic: `embedding` holds the chunk vector as packed little-endian
#     float32 bytes (np.float32.tobytes()); cosine search is brute-force in
#     Python — at this corpus size (low thousands of chunks) that is fast and
#     avoids any vector-DB dependency.
# Embedding is optional (KB_EMBEDDINGS=off leaves `embedding` NULL and search
# falls back to keyword-only).


KB_SOURCE_TYPES = ("ticket", "forum", "chat", "doc", "code")


class KBDocument(Base):
    __tablename__ = "kb_documents"
    __table_args__ = (
        UniqueConstraint("source_type", "source_ref", name="uq_kb_doc_source"),
        Index("idx_kb_doc_type", "source_type"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # ticket | forum | chat | doc | code (KB_SOURCE_TYPES)
    source_type = Column(String(16), nullable=False)
    # ticket id / forum post (thread) id / f"{channel_id}:{YYYYMM}" / repo-relative file path
    source_ref = Column(String(191), nullable=False)
    title = Column(String(255), nullable=True)
    url = Column(String(512), nullable=True)
    author_label = Column(String(255), nullable=True)
    started_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True, default=func.now(), onupdate=func.now())
    # Free-form JSON: content hash (docs), max mirrored row id (tickets),
    # last message id (forum threads), message counts, etc. — whatever the
    # miner needs for cheap change detection on re-runs.
    meta_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())


class KBChunk(Base):
    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_kb_chunk_doc_idx"),
        # FULLTEXT index for MATCH ... AGAINST keyword retrieval.
        Index("ft_kb_chunks_content", "content", mysql_prefix="FULLTEXT"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        Integer, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    # Rough size estimate (len(content)//4) so prompt budgets can be planned
    # without a tokenizer dependency.
    token_estimate = Column(Integer, nullable=True)
    # Packed little-endian float32 vector (np.ndarray.tobytes()); NULL until
    # embed_missing_chunks() processes the row or when KB_EMBEDDINGS=off.
    embedding = Column(LargeBinary, nullable=True)
    embedding_model = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=func.now())


class KBIngestState(Base):
    """Per-source incremental cursor for the miner.

    source_ref examples: "chat:<channel_id>", "forum:<forum_channel_id>",
    "tickets", "docs". For chat channels last_message_id is the highest
    Discord snowflake processed — re-runs fetch history(after=that id) only.
    """

    __tablename__ = "kb_ingest_state"
    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_kb_ingest_source"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_ref = Column(String(191), nullable=False)
    last_message_id = Column(String(32), nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="idle")
    detail = Column(Text, nullable=True)
