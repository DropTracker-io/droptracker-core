"""kb01a: knowledgebase tables (kb_documents, kb_chunks, kb_ingest_state)

Adds the Discord-mined knowledgebase storage:
- kb_documents: one row per source unit (ticket / forum thread / chat
  channel-month / doc file)
- kb_chunks: retrievable text chunks with a FULLTEXT(content) index (first
  FULLTEXT index in this schema — MariaDB 10.11 InnoDB) and an optional packed
  float32 embedding BLOB for local semantic search
- kb_ingest_state: per-source incremental cursors for the miner

Revision ID: kb01a_knowledgebase_tables
Revises: web67a_ticket_inactivity
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "kb01a_knowledgebase_tables"
down_revision = "web67a_ticket_inactivity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.String(length=191), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=True),
        sa.Column("author_label", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_ref", name="uq_kb_doc_source"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_kb_doc_type", "kb_documents", ["source_type"])

    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=True),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["kb_documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_kb_chunk_doc_idx"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(
        "ft_kb_chunks_content", "kb_chunks", ["content"], mysql_prefix="FULLTEXT"
    )

    op.create_table(
        "kb_ingest_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_ref", sa.String(length=191), nullable=False),
        sa.Column("last_message_id", sa.String(length=32), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="idle"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_ref", name="uq_kb_ingest_source"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def downgrade() -> None:
    op.drop_table("kb_chunks")
    op.drop_table("kb_ingest_state")
    op.drop_table("kb_documents")
