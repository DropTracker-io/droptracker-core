"""discord_audit_configs + discord_message_log (web161a).

Discord server audit log: per-server settings and the bot's short-lived copy
of human messages, so a deleted or edited message can be shown to the
server's admins. New tables, no backfill.

Revision ID: web161a_discord_audit
Revises: web160a_event_guests
"""
from alembic import op
import sqlalchemy as sa

revision = "web161a_discord_audit"
down_revision = "web160a_event_guests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "discord_audit_configs" not in tables:
        op.create_table(
            "discord_audit_configs",
            sa.Column("guild_id", sa.String(32), primary_key=True),
            sa.Column("group_id", sa.Integer(),
                      sa.ForeignKey("groups.group_id", ondelete="SET NULL"), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("channel_id", sa.String(32), nullable=True),
            sa.Column("events", sa.JSON(), nullable=True),
            sa.Column("ignored_channel_ids", sa.JSON(), nullable=True),
            sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("updated_by", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            mysql_charset="utf8mb4",
        )
    if "discord_message_log" not in tables:
        op.create_table(
            "discord_message_log",
            sa.Column("message_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("channel_id", sa.BigInteger(), nullable=False),
            sa.Column("author_id", sa.BigInteger(), nullable=False),
            sa.Column("author_name", sa.String(100), nullable=True),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("attachments", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("edited_at", sa.DateTime(), nullable=True),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            mysql_charset="utf8mb4",
        )
        op.create_index("idx_discord_message_log_guild_created", "discord_message_log",
                        ["guild_id", "created_at"])
        op.create_index("idx_discord_message_log_author", "discord_message_log", ["author_id"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "discord_message_log" in tables:
        op.drop_table("discord_message_log")
    if "discord_audit_configs" in tables:
        op.drop_table("discord_audit_configs")
