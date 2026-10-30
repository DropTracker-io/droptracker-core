"""Discord delivery relay: a group's own bot posts DropTracker's messages (web153a).

- ``discord_relay_integrations`` — one customer bot: owning group, its
  Discord application id, the bound data-API key, the features it takes over
  (JSON list), active/paused, and how long undelivered messages wait.
- ``discord_relay_guilds`` — the servers it delivers for. ``guild_id`` is
  UNIQUE so a server can never have two relay owners.

No bot tokens are stored anywhere. Delivery state lives in Redis (relay:*).

Revision ID: web153a_discord_relay
Revises: web152a_event_roster_sync_chat_tags
"""
from alembic import op
import sqlalchemy as sa

revision = "web153a_discord_relay"
down_revision = "web152a_event_roster_sync_chat_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discord_relay_integrations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=False),
        sa.Column("label", sa.String(64), nullable=False, server_default=""),
        sa.Column("application_id", sa.String(32), nullable=True),
        sa.Column("api_key_id", sa.BigInteger(), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="paused"),
        sa.Column("hold_minutes", sa.Integer(), nullable=False, server_default="360"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_discord_relay_group", "discord_relay_integrations", ["group_id"])
    op.create_index("uq_discord_relay_api_key", "discord_relay_integrations",
                    ["api_key_id"], unique=True)
    op.create_table(
        "discord_relay_guilds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("integration_id", sa.Integer(),
                  sa.ForeignKey("discord_relay_integrations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_discord_relay_guild_integration", "discord_relay_guilds",
                    ["integration_id"])
    op.create_index("uq_discord_relay_guild", "discord_relay_guilds", ["guild_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_discord_relay_guild", table_name="discord_relay_guilds")
    op.drop_index("idx_discord_relay_guild_integration", table_name="discord_relay_guilds")
    op.drop_table("discord_relay_guilds")
    op.drop_index("uq_discord_relay_api_key", table_name="discord_relay_integrations")
    op.drop_index("idx_discord_relay_group", table_name="discord_relay_integrations")
    op.drop_table("discord_relay_integrations")
