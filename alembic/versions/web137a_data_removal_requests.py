"""Self-serve data removal: data_removal_requests (web137a).

One row per request to erase a player or a group, from the site button
through staff verification in a ticket to the purge. See
db/models/data_removal.py.

Revision ID: web137a_data_removal_requests
Revises: data02_drops_index_cleanup
"""
import sqlalchemy as sa
from alembic import op

revision = "web137a_data_removal_requests"
down_revision = "data02_drops_index_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_removal_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("subject_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="awaiting_verification"),
        sa.Column("requested_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True),
        sa.Column("ticket_id", sa.Integer(), nullable=True),
        sa.Column("verification_phrase", sa.String(64), nullable=True),
        sa.Column("decided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("purge_after", sa.DateTime(), nullable=True),
        sa.Column("prior_hidden", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("export_path", sa.String(300), nullable=True),
        sa.Column("export_bytes", sa.BigInteger(), nullable=True),
        sa.Column("export_expires_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_data_removal_subject", "data_removal_requests", ["kind", "subject_id"])
    op.create_index("idx_data_removal_due", "data_removal_requests", ["status", "purge_after"])


def downgrade() -> None:
    op.drop_table("data_removal_requests")
