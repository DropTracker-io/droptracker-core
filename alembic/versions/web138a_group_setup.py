"""Group setup health: groups.created_via, guild_bot_events, group_setup_alerts (web138a).

See db/models/group_setup.py. created_via records which sign-up path made a
group; guild_bot_events records the bot joining and leaving servers;
group_setup_alerts is the setup-alert state the core bot keeps.

Revision ID: web138a_group_setup
Revises: web137a_page_layouts
"""
import sqlalchemy as sa
from alembic import op

revision = "web138a_group_setup"
down_revision = "web137a_page_layouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("groups", sa.Column("created_via", sa.String(16), nullable=True))
    op.create_table(
        "guild_bot_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("event", sa.String(8), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_guild_bot_events_guild", "guild_bot_events", ["guild_id", "at"])
    op.create_table(
        "group_setup_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("subject", sa.String(40), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("guild_id", sa.String(32), nullable=True),
        sa.Column("state", sa.String(12), nullable=False, server_default="open"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("strikes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("send_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("recipient_discord_id", sa.String(35), nullable=True),
        sa.Column("recipient_role", sa.String(12), nullable=True),
        sa.Column("delivered", sa.Boolean(), nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("kind", "subject", name="uq_group_setup_alerts_kind_subject"),
    )
    op.create_index("ix_group_setup_alerts_group", "group_setup_alerts", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_group_setup_alerts_group", "group_setup_alerts")
    op.drop_table("group_setup_alerts")
    op.drop_index("ix_guild_bot_events_guild", "guild_bot_events")
    op.drop_table("guild_bot_events")
    op.drop_column("groups", "created_via")
