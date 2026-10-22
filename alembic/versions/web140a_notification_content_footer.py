"""Notification message text + footers (web140a).

``group_embeds.content``: the message text sent above an embed (NULL = the
built-in line). ``group_notification_footers``: a group's footer choice for
every type ("") or one type. See db/models/group_notification_footer.py.

Revision ID: web140a_notification_content_footer
Revises: web139a_group_bot_profiles
"""
import sqlalchemy as sa
from alembic import op

revision = "web140a_notification_content_footer"
down_revision = "web139a_group_bot_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_embeds", sa.Column("content", sa.String(2000), nullable=True))
    op.create_table(
        "group_notification_footers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Integer(),
                  sa.ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=False),
        sa.Column("notification_type", sa.String(16), nullable=False, server_default=""),
        sa.Column("mode", sa.String(8), nullable=False, server_default="default"),
        sa.Column("text", sa.String(2048), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.UniqueConstraint("group_id", "notification_type", name="uq_group_notification_footer"),
    )


def downgrade() -> None:
    op.drop_table("group_notification_footers")
    op.drop_column("group_embeds", "content")
