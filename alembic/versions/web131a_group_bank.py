"""group_bank_entries (web131a).

The clan bank: a per-group GP ledger of donations, withdrawals, payouts,
transfers into event prize pots and staff adjustments. Signed ``amount``; the
balance is the sum of confirmed rows. Read/written by
web_api/routes/group_bank.py through services/group_bank.py.

New table only, so it is safe to apply live. Guarded, so a re-run is harmless.

Revision ID: web131a_group_bank
Revises: web130a_notice_delivery
"""
from alembic import op
import sqlalchemy as sa

revision = "web131a_group_bank"
down_revision = "web130a_notice_delivery"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("group_bank_entries"):
        return
    op.create_table(
        "group_bank_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="confirmed"),
        sa.Column("source", sa.String(16), nullable=False, server_default="staff"),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.Column("rsn", sa.String(24), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("review_note", sa.String(255), nullable=True),
        sa.Column("proof_url", sa.String(255), nullable=True),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("event_buyin_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("acted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["acted_by_user_id"], ["users.user_id"]),
    )
    op.create_index("idx_gbank_group_status", "group_bank_entries", ["group_id", "status"])
    op.create_index("idx_gbank_group_created", "group_bank_entries", ["group_id", "created_at"])


def downgrade():
    op.drop_table("group_bank_entries")
