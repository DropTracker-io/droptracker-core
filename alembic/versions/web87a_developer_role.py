"""users.is_moderator -> users.is_developer — Moderator role becomes Developer.

The Developer role supersedes Moderator: same trusted-helper mutation tools
(pb-blocks, item values, task library) plus a read-mostly diagnostic surface
(audit log with payload redaction, restricted read-only data viewer, app logs,
service status, status board, dev tracker, lookup). Existing moderators are
carried over. The "moderator" profile badge is re-keyed to "developer" in
place so existing per-player awards keep their badge_id/slot rows.

Revision ID: web87a_developer_role
Revises: web86a_group_owner_invariant
"""
from alembic import op
import sqlalchemy as sa

revision = "web87a_developer_role"
down_revision = "web86a_group_owner_invariant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "is_moderator",
        new_column_name="is_developer",
        existing_type=sa.Boolean(),
        existing_nullable=True,
        existing_server_default=sa.text("0"),
    )
    op.execute(
        "UPDATE badges SET `key` = 'developer', name = 'Developer', "
        "description = 'DropTracker site developer.' WHERE `key` = 'moderator'"
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "is_developer",
        new_column_name="is_moderator",
        existing_type=sa.Boolean(),
        existing_nullable=True,
        existing_server_default=sa.text("0"),
    )
    op.execute(
        "UPDATE badges SET `key` = 'moderator', name = 'Moderator', "
        "description = 'DropTracker site moderator.' WHERE `key` = 'developer'"
    )
