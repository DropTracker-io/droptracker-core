"""web_event_guilds: per-(event, guild) Discord scheduled-event sync state.

Desired-state mirror for Discord scheduled events: the Web API writes rows
(`pending` / `delete_pending`) and never talks to Discord; the core bot's
`reconcile_event_scheduled_events` task (bots/main.py) creates/edits/deletes
the real Discord events and writes back `discord_scheduled_event_id`.
See services/event_scheduled_events.py.

Also the merge point for the two open heads (web28a_subscription_pool and
web29a_suggestion_forum both fork from web27a_tier_flair); both are already
applied, so this collapses the version table back to a single head.
"""

from alembic import op
import sqlalchemy as sa


revision = "web30a_event_guilds"
down_revision = ("web28a_subscription_pool", "web29a_suggestion_forum")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_guilds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("discord_scheduled_event_id", sa.String(length=32), nullable=True),
        # pending|synced|delete_pending|failed
        sa.Column("sync_status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_web_event_guild", "web_event_guilds", ["event_id", "guild_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_web_event_guild", table_name="web_event_guilds")
    op.drop_table("web_event_guilds")
