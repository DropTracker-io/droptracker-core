"""web_events.roster_clan_sync + team_chat_tags (web152a).

``roster_clan_sync``: keep the event's roster in step with the clan. While on,
the lifecycle sweep enters every current clan member (the same rules as the
"Sign up all clan members" button) and takes out anyone who has left the clan,
so the roster follows the WOM-synced membership without a manager re-running
anything. Off for every existing event.

``team_chat_tags``: whether the RuneLite plugin badges teammates in clan chat
with their team's tag/colour. A free-for-all event gives every player a team of
their own, so a tag per player is noise; turning this off withholds the roster
stamp from /event_state and the event from /event_roster, and the plugin draws
nothing. On for every existing event, so nothing changes until someone flips it.

Revision ID: web152a_event_roster_sync_chat_tags
Revises: web151a_hardcore_players_world
"""
from alembic import op
import sqlalchemy as sa

revision = "web152a_event_roster_sync_chat_tags"
down_revision = "web151a_hardcore_players_world"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column("roster_clan_sync", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.add_column(
        "web_events",
        sa.Column("team_chat_tags", sa.Boolean(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("web_events", "team_chat_tags")
    op.drop_column("web_events", "roster_clan_sync")
