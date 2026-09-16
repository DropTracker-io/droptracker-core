"""Per-player monthly EHB gains harvested from WOM (web83a).

Recap cards report EHB earned *during* a month, and the previous month's figure
beside it. Neither can be derived from what we already store: ``players.ehb`` is
a lifetime total that is overwritten on every roster sync, so it carries no
history to difference.

So the gain is harvested once per closed month — one bulk-gained call per
WOM-linked clan covers its whole roster — and kept here. A closed month's gains
are immutable, which makes these rows a permanent cache rather than something
that needs refreshing: next month's card reads its "previous" figure straight
out of this table, and the lazily-generated player cards read it without going
near the WOM rate limiter.

Revision ID: web83a_recap_wom_gains
Revises: web82a_event_schedules
"""
from alembic import op
import sqlalchemy as sa

revision = "web83a_recap_wom_gains"
down_revision = "web82a_event_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recap_wom_gains",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("ehb_gained", sa.Float(), nullable=False, server_default="0"),
        # 'bulk' (one call covered the whole clan) or 'player' (per-player
        # fallback for someone in no WOM-linked group). Kept so a thin month can
        # be explained rather than guessed at.
        sa.Column("source", sa.String(16), nullable=False, server_default="bulk"),
        sa.Column(
            "fetched_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("player_id", "period"),
    )
    # "has this month been harvested yet" — the idempotence check the delivery
    # sweep runs every 15 minutes for three days.
    op.create_index("idx_recap_wom_gains_period", "recap_wom_gains", ["period"])


def downgrade() -> None:
    op.drop_index("idx_recap_wom_gains_period", table_name="recap_wom_gains")
    op.drop_table("recap_wom_gains")
