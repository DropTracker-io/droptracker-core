"""Add entry_id to player_points.

This is defensive: some environments may already have the column.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = "9f0c1d2e3a4b"
down_revision = "6c0b93fe767a"
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


def upgrade() -> None:
    if not _column_exists("player_points", "entry_id"):
        op.add_column("player_points", sa.Column("entry_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    if _column_exists("player_points", "entry_id"):
        op.drop_column("player_points", "entry_id")

