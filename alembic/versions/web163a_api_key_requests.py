"""api_key_requests (web163a).

One row per request for a Data API key, made from a group's API page or a
user's own, through the support ticket where staff approve or deny it. See
db/models/api_key_requests.py. New table, no backfill.

Revision ID: web163a_api_key_requests
Revises: web162a_stats_shadow_diffs
"""
import sqlalchemy as sa
from alembic import op

revision = "web163a_api_key_requests"
down_revision = "web162a_stats_shadow_diffs"
branch_labels = None
depends_on = None

TABLE = "api_key_requests"


def upgrade() -> None:
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("requested_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True),
        sa.Column("label", sa.String(64), nullable=False, server_default=""),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("ticket_id", sa.Integer(), nullable=True),
        sa.Column("api_key_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_api_key_requests_status", TABLE, ["status", "id"])
    op.create_index("idx_api_key_requests_user", TABLE, ["requested_by_user_id"])
    op.create_index("idx_api_key_requests_group", TABLE, ["group_id"])
    op.create_index("idx_api_key_requests_ticket", TABLE, ["ticket_id"])


def downgrade() -> None:
    op.drop_table(TABLE)
