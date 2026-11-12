"""stats_shadow_diffs (web162a).

Where the shadow comparison is kept: for each consumer that is moving off
Wise Old Man (events, EHB rates, a player's EHB...), the number WOM gave next
to the number our own stat store gives, per player and metric. One row per
(consumer, context, player, metric), updated in place each time it is
compared, so the table is bounded by what is being compared rather than by
how often. Read by ``scripts/stats_shadow_report.py``. New table, no backfill.

Revision ID: web162a_stats_shadow_diffs
Revises: web161a_player_stat_snapshots
"""
from alembic import op
import sqlalchemy as sa

revision = "web162a_stats_shadow_diffs"
down_revision = "web161a_player_stat_snapshots"
branch_labels = None
depends_on = None

TABLE = "stats_shadow_diffs"


def upgrade() -> None:
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("consumer", sa.String(24), nullable=False),
        sa.Column("context", sa.String(64), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("wom_value", sa.Float(asdecimal=False), nullable=True),
        sa.Column("our_value", sa.Float(asdecimal=False), nullable=True),
        sa.Column("our_source", sa.String(24), nullable=True),
        sa.Column("our_age_seconds", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(255), nullable=True),
        sa.Column("observations", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("worst_delta", sa.Float(asdecimal=False), nullable=True),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("consumer", "context", "player_id", "metric",
                            name="uq_stats_shadow_subject"),
    )
    op.create_index("ix_stats_shadow_consumer_seen", TABLE, ["consumer", "last_seen"])


def downgrade() -> None:
    op.drop_table(TABLE)
