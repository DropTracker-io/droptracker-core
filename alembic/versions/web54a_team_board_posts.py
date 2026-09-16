"""Team-channel primary board posts (web54a).

Each auto-created team channel/thread gets a bot-maintained "primary post":
the team-filtered live board image plus quick links (standings channel,
interactive Activity). Post once, edit in place — ``board_message_id`` is the
message, ``board_state_hash`` the last rendered board signature (the change
detector), ``board_updated_at`` the last edit stamp.

Revision ID: web54a_team_board_posts
Revises: web53a_event_team_discord
"""
from alembic import op
import sqlalchemy as sa

revision = "web54a_team_board_posts"
down_revision = "web53a_event_team_discord"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_event_team_discord",
                  sa.Column("board_message_id", sa.String(32), nullable=True))
    op.add_column("web_event_team_discord",
                  sa.Column("board_state_hash", sa.String(64), nullable=True))
    op.add_column("web_event_team_discord",
                  sa.Column("board_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("web_event_team_discord", "board_updated_at")
    op.drop_column("web_event_team_discord", "board_state_hash")
    op.drop_column("web_event_team_discord", "board_message_id")
