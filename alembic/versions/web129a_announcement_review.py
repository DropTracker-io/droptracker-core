"""Owner review for site-wide announcements (web129a).

A global announcement not written by an approver (the owner by default, see
``services/leader_updates.py``) is saved as ``status='draft'`` and waits in
the review queue on /admin/announcements. Nothing about it is public, on the
site or on Discord, until an approver edits/approves it. These columns carry
what the approval needs:

* ``post_to_discord`` - the author's "Also post to Discord" choice, applied
  only when the post is approved.
* ``source_label`` - who or what drafted it ("weekly roundup", "agent", a
  staff name), shown in the queue.
* ``reviewed_by`` / ``reviewed_at`` - the approver and when.

Additive and nullable/defaulted, so it is safe to apply live. Guarded, so a
re-run is harmless.

Revision ID: web129a_announcement_review
Revises: web128a_admin_widget_tokens
"""
from alembic import op
import sqlalchemy as sa

revision = "web129a_announcement_review"
down_revision = "web128a_admin_widget_tokens"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("post_to_discord", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    sa.Column("source_label", sa.String(64), nullable=True),
    sa.Column("reviewed_by", sa.Integer(), nullable=True),
    sa.Column("reviewed_at", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns("announcements")}
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("announcements", column.copy())


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("announcements", column.name)
