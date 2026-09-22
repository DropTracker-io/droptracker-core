"""popup_notices + popup_notice_receipts: targeted site pop-ups (web118a).

Staff write a Markdown notice in /admin/notices and pick who should see it:
specific users, clan leaders, group members, supporters on given tiers, staff,
or everyone signed in. The audience is stored as rules and matched against each
visitor when they load the site, so nothing is fanned out per recipient.

``popup_notice_receipts`` holds one row per (notice, user) once that user has
been shown the notice. ``dismissed_at`` is what stops it from coming back, on
every device, and the same rows give staff their seen/closed counts.

Purely additive: two new tables, nothing existing changes.

Revision ID: web118a_popup_notices
Revises: web117a_ticket_autoclose_exempt
"""
from alembic import op
import sqlalchemy as sa

revision = "web118a_popup_notices"
down_revision = "web117a_ticket_autoclose_exempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "popup_notices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("cta_label", sa.String(length=40), nullable=True),
        sa.Column("cta_url", sa.String(length=512), nullable=True),
        sa.Column("tone", sa.String(length=12), nullable=False, server_default="info"),
        sa.Column("size", sa.String(length=8), nullable=False, server_default="md"),
        sa.Column("audience_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="draft"),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("audience_estimate", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_popup_notice_status", "popup_notices", ["status", "starts_at"], unique=False)

    op.create_table(
        "popup_notice_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("notice_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["notice_id"], ["popup_notices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notice_id", "user_id", name="uix_popup_notice_receipt"),
    )
    op.create_index("idx_popup_receipt_user", "popup_notice_receipts", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_popup_receipt_user", table_name="popup_notice_receipts")
    op.drop_table("popup_notice_receipts")
    op.drop_index("idx_popup_notice_status", table_name="popup_notices")
    op.drop_table("popup_notices")
