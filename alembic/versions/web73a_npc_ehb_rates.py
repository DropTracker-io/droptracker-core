"""Derived EHB rates for WOM-unpriced bosses (web73a).

``npc_ehb_rates``: kills-per-hour estimates computed from DropTracker's own
data (``scripts/compute_npc_ehb_rates.py``), consulted by Bingo EHB pricing
only when WOM's ``/efficiency/rates`` table has no entry for the boss. On the
2026-07-28 backfill, 69% of all effort kills (Maggot King, Zalcano) were
priced 0 EHB purely because WOM hadn't published a rate.

A derived rate never overrides a WOM one, and hours priced with one are
flagged ``estimated`` in every payload so the UI can label them.

Revision ID: web73a_npc_ehb_rates
Revises: web72a_event_effort
"""
from alembic import op
import sqlalchemy as sa

revision = "web73a_npc_ehb_rates"
down_revision = "web72a_event_effort"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "npc_ehb_rates",
        sa.Column("npc_id", sa.Integer(), sa.ForeignKey("npc_list.npc_id"),
                  primary_key=True, autoincrement=False),
        # WOM slug when the NPC has one (rate simply unpublished); NULL for
        # sources WOM doesn't track at all.
        sa.Column("boss_metric", sa.String(48), nullable=True),
        sa.Column("rate_kph", sa.Float(), nullable=False),
        # PB rows (pb_median) or qualifying players (drop_gaps) behind it.
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("npc_ehb_rates")
