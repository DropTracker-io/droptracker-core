"""Custom bot profile: group_bot_profiles (web139a).

See db/models/group_bot_profile.py. One row per group holding the bot
nickname, bio, avatar and banner the group asked for, next to what the core
bot last applied in its Discord server.

Revision ID: web139a_group_bot_profiles
Revises: web138a_group_setup
"""
import sqlalchemy as sa
from alembic import op

revision = "web139a_group_bot_profiles"
down_revision = "web138a_group_setup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_bot_profiles",
        sa.Column("group_id", sa.Integer(),
                  sa.ForeignKey("groups.group_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("guild_id", sa.String(32), nullable=True),
        sa.Column("nick", sa.String(32), nullable=True),
        sa.Column("bio", sa.String(190), nullable=True),
        sa.Column("avatar_url", sa.String(512), nullable=True),
        sa.Column("banner_url", sa.String(512), nullable=True),
        sa.Column("applied_nick", sa.String(32), nullable=True),
        sa.Column("applied_bio", sa.String(190), nullable=True),
        sa.Column("applied_avatar_url", sa.String(512), nullable=True),
        sa.Column("applied_banner_url", sa.String(512), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column("retry_after", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("group_bot_profiles")
