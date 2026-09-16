"""Add group_point_blacklist table."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = "7b6e4af4b2c1"
down_revision = "1f3c9a7b2d10"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    result = bind.execute(
        text(
            """
            SELECT COUNT(*) AS cnt
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table_name
            """
        ),
        {"table_name": table_name},
    ).scalar()
    try:
        return int(result or 0) > 0
    except Exception:
        return False


def upgrade() -> None:
    if _table_exists("group_point_blacklist"):
        return
    op.create_table(
        "group_point_blacklist",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("list_type", sa.String(length=10), nullable=False, server_default="whitelist"),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("npc_id", sa.Integer(), nullable=True),
        sa.Column("date_added", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("date_updated", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_group_point_blacklist_group_id", "group_point_blacklist", ["group_id"], unique=False)
    op.create_index("ix_group_point_blacklist_item_id", "group_point_blacklist", ["item_id"], unique=False)
    op.create_index("ix_group_point_blacklist_npc_id", "group_point_blacklist", ["npc_id"], unique=False)
    op.create_index("ix_group_point_blacklist_list_type", "group_point_blacklist", ["list_type"], unique=False)


def downgrade() -> None:
    if not _table_exists("group_point_blacklist"):
        return
    op.drop_index("ix_group_point_blacklist_list_type", table_name="group_point_blacklist")
    op.drop_index("ix_group_point_blacklist_npc_id", table_name="group_point_blacklist")
    op.drop_index("ix_group_point_blacklist_item_id", table_name="group_point_blacklist")
    op.drop_index("ix_group_point_blacklist_group_id", table_name="group_point_blacklist")
    op.drop_table("group_point_blacklist")

