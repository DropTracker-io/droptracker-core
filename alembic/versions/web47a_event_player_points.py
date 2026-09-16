"""Per-player event contribution points.

``web_event_player_points`` — one row per (task, team, player), written when a
task completes: the task's points split across the applied-ledger contributors
by net quantity share (FLOAT — a 5-point task done 50/50 awards 2.5 each).
Rewritten/deleted when a revoke changes the task's completion state. Team
score remains the integer competitive total; this is the per-player stat that
lets end-of-event points correlate with contribution.

Revision ID: web47a_event_player_points
Revises: web45a_board_game_economy
"""
from alembic import op
import sqlalchemy as sa

revision = "web47a_event_player_points"
down_revision = "web45a_board_game_economy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_player_points",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("web_event_tasks.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("points", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_web_evt_player_points", "web_event_player_points",
        ["task_id", "team_id", "player_id"], unique=True,
    )
    op.create_index(
        "idx_web_evt_player_points_event", "web_event_player_points",
        ["event_id", "player_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_web_evt_player_points_event", table_name="web_event_player_points")
    op.drop_index("uq_web_evt_player_points", table_name="web_event_player_points")
    op.drop_table("web_event_player_points")
