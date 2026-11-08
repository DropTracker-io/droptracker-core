"""group_bank_entries.holder_player_id / holder_rsn (web159a).

Clan bank holders (suggestion #160 follow-up): clans keep GP with their staff
because the in-game coffer holds at most 50M. A row's holder is whose keeping
it moved GP into or out of: a withdrawal they hold, a deposit they returned,
a payout or event they paid from, or a ``holder_transfer`` they passed on
(a new kind; ``kind`` is VARCHAR(16), so no DDL for it). Both nullable, no
backfill: existing rows have no holder and keep counting as before.

Revision ID: web159a_bank_holders
Revises: web158a_bank_outside_events
"""
from alembic import op
import sqlalchemy as sa

revision = "web159a_bank_holders"
down_revision = "web158a_bank_outside_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("group_bank_entries")}
    if "holder_player_id" not in columns:
        op.add_column("group_bank_entries", sa.Column("holder_player_id", sa.Integer(), nullable=True))
    if "holder_rsn" not in columns:
        op.add_column("group_bank_entries", sa.Column("holder_rsn", sa.String(24), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("group_bank_entries")}
    for name in ("holder_rsn", "holder_player_id"):
        if name in columns:
            op.drop_column("group_bank_entries", name)
