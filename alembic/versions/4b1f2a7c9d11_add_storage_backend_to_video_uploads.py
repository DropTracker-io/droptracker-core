"""Add storage_backend to video_uploads.

Revision ID: 4b1f2a7c9d11
Revises: 9f0c1d2e3a4b
Create Date: 2026-02-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = "4b1f2a7c9d11"
down_revision = "9f0c1d2e3a4b"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    result = bind.execute(
        text(
            """
            SELECT COUNT(*) AS cnt
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table_name
              AND COLUMN_NAME = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).scalar()
    try:
        return int(result or 0) > 0
    except Exception:
        return False


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    result = bind.execute(
        text(
            """
            SELECT COUNT(*) AS cnt
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table_name
              AND INDEX_NAME = :index_name
            """
        ),
        {"table_name": table_name, "index_name": index_name},
    ).scalar()
    try:
        return int(result or 0) > 0
    except Exception:
        return False


def upgrade() -> None:
    if not _column_exists("video_uploads", "storage_backend"):
        op.add_column(
            "video_uploads",
            sa.Column("storage_backend", sa.String(length=20), nullable=False, server_default="b2"),
        )
    if not _index_exists("video_uploads", "idx_video_storage_backend"):
        op.create_index("idx_video_storage_backend", "video_uploads", ["storage_backend"], unique=False)


def downgrade() -> None:
    if _index_exists("video_uploads", "idx_video_storage_backend"):
        op.drop_index("idx_video_storage_backend", table_name="video_uploads")
    if _column_exists("video_uploads", "storage_backend"):
        op.drop_column("video_uploads", "storage_backend")

