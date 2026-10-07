"""Pop-up notices as the one place updates are written and approved (web130a).

/admin/notices becomes the composer for every staff update. Besides the
targeted pop-up, a notice can also go out as a news post (the public
/announcements page, an ``announcements`` row) and, through that post, to the
Discord news channel (``services/leader_updates.py``). And like site-wide
posts (web129a), a notice sent by anyone but an approver waits for the owner:
``status='review'`` until approved there.

* ``show_popup`` / ``publish_post`` / ``post_to_discord`` - where it goes.
  Existing rows are pop-ups only, which is what the defaults say.
* ``announcement_id`` - the news post made when it went out.
* ``source_label`` - who or what drafted it, for the review queue.
* ``reviewed_by`` / ``reviewed_at`` - the approver and when.

Additive and defaulted, so it is safe to apply live. Guarded, so a re-run is
harmless.

Revision ID: web130a_notice_delivery
Revises: web129a_announcement_review
"""
from alembic import op
import sqlalchemy as sa

revision = "web130a_notice_delivery"
down_revision = "web129a_announcement_review"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("show_popup", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    sa.Column("publish_post", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    sa.Column("post_to_discord", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    sa.Column("announcement_id", sa.Integer(), nullable=True),
    sa.Column("source_label", sa.String(64), nullable=True),
    sa.Column("reviewed_by", sa.Integer(), nullable=True),
    sa.Column("reviewed_at", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns("popup_notices")}
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("popup_notices", column.copy())


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("popup_notices", column.name)
