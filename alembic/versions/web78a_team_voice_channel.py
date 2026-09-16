"""Per-team temporary voice channels (web78a).

``web_event_team_discord.voice_channel_id``: snowflake of the optional
auto-created team VOICE channel (config ``voice_enabled``), written back by
the core bot reconciler. Same access model (private to the team role) and the
same teardown rules (immediate on removal, 48h grace on natural end, orphan
queue on hard delete) as the team text channel.

Revision ID: web78a_team_voice_channel
Revises: web77a_recap_snapshots
"""
from alembic import op
import sqlalchemy as sa

revision = "web78a_team_voice_channel"
down_revision = "web77a_recap_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_team_discord",
        sa.Column("voice_channel_id", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_event_team_discord", "voice_channel_id")
