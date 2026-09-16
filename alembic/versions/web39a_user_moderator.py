"""users.is_moderator — trusted-helper flag for the web moderation panel.

Moderators get access to the /moderation panel (pb-blocks, item values,
event task library); superadmin implies moderator everywhere. Granted and
revoked from /admin/users (audited), which also awards/revokes the
"moderator" profile badge on the user's player accounts.

Revision ID: web39a_user_moderator
Revises: web38a_event_signups
"""
from alembic import op
import sqlalchemy as sa

revision = "web39a_user_moderator"
down_revision = "web38a_event_signups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_moderator", sa.Boolean(), nullable=True, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("users", "is_moderator")
