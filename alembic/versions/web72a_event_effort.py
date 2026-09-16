"""Bingo EHB — event-scoped effort tracking (web72a).

``web_event_effort``: kills a player put into an event's relevant NPCs, one row
per (event, player, npc), recorded whether or not anything dropped. Contribution
counters only ever record credit, so a player who grinds a boss all week for a
tile and gets nothing is currently invisible on every event surface.

Deliberately its own table: ``web_event_progress`` is per (task, team) and
carries no ``player_id``, and ``web_event_completions`` is the credit ledger —
effort must never inflate contribution counts, points or the "Last …" line.

``frozen_at`` is stamped once every task an NPC feeds has completed for the
team, after which the row stops accruing (banked kills stay). See
``services/event_effort.py``.

Revision ID: web72a_event_effort
Revises: web70a_event_signup_close
"""
from alembic import op
import sqlalchemy as sa

revision = "web72a_event_effort"
down_revision = "web70a_event_signup_close"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_effort",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        # Stamped from the roster at write time so the admin inactivity report
        # can group by team without a join.
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("npc_id", sa.Integer(), sa.ForeignKey("npc_list.npc_id"), nullable=False),
        # WOM boss slug, denormalized so the read path prices EHB without
        # re-resolving names. NULL = tracked activity with no WOM rate (0 EHB).
        sa.Column("boss_metric", sa.String(48), nullable=True),
        sa.Column("kills", sa.BigInteger(), nullable=False, server_default="0"),
        # 'plugin' | 'wom' | 'both' — which side of the hybrid fold fed the row.
        sa.Column("source", sa.String(16), nullable=False, server_default="plugin"),
        sa.Column("first_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("frozen_at", sa.DateTime(), nullable=True),
    )
    # One row per player per NPC per event — effort is not per task, because an
    # NPC feeding three tiles is still one in-game kill counter.
    op.create_index(
        "uq_web_evt_effort", "web_event_effort",
        ["event_id", "player_id", "npc_id"], unique=True,
    )
    op.create_index(
        "idx_web_evt_effort_team", "web_event_effort", ["event_id", "team_id"],
    )
    # The inactivity report sorts a whole event's roster by recency.
    op.create_index(
        "idx_web_evt_effort_last", "web_event_effort", ["event_id", "last_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_web_evt_effort_last", table_name="web_event_effort")
    op.drop_index("idx_web_evt_effort_team", table_name="web_event_effort")
    op.drop_index("uq_web_evt_effort", table_name="web_event_effort")
    op.drop_table("web_event_effort")
