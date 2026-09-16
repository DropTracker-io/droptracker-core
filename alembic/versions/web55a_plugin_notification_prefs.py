"""Per-player website prefs for in-game plugin notifications (web55a).

Backing table for the event → plugin in-game notification feature
(docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md). One row per player; ``prefs`` is a
JSON object mapping notification type → bool, absent row/keys = enabled.

Revision ID: web55a_plugin_notification_prefs
Revises: web54a_team_board_posts
"""
from alembic import op
import sqlalchemy as sa

revision = "web55a_plugin_notification_prefs"
down_revision = "web54a_team_board_posts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_notification_prefs",
        sa.Column("player_id", sa.Integer(),
                  sa.ForeignKey("players.player_id"), primary_key=True),
        sa.Column("prefs", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("player_notification_prefs")
