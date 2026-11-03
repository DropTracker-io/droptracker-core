"""feedback_requests + feedback_receipts + feedback_responses: clan leader
feedback (web155a).

Staff ask chosen people for feedback from /admin/feedback (audience rules as
for pop-up notices). Answers are stored here and posted to the staff Discord,
where they can be published to the testimonials channel.

Revision ID: web155a_feedback
Revises: web154a_group_stats_recap_designs
"""
from alembic import op
import sqlalchemy as sa

revision = "web155a_feedback"
down_revision = "web154a_group_stats_recap_designs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("audience_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
        sa.Column("audience_estimate", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_feedback_request_status", "feedback_requests", ["status"])

    op.create_table(
        "feedback_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(), nullable=True),
        sa.Column("declined_at", sa.DateTime(), nullable=True),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["feedback_requests.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("request_id", "user_id", name="uix_feedback_receipt"),
    )
    op.create_index("idx_feedback_receipt_user", "feedback_receipts", ["user_id"])

    op.create_table(
        "feedback_responses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(12), nullable=True),
        sa.Column("group_tier", sa.String(32), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("liked_text", sa.Text(), nullable=True),
        sa.Column("improve_text", sa.Text(), nullable=True),
        sa.Column("quote_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attribution", sa.String(8), nullable=False, server_default="clan"),
        sa.Column("contact_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(12), nullable=False, server_default="new"),
        sa.Column("raw_message_id", sa.String(32), nullable=True),
        sa.Column("published_message_id", sa.String(32), nullable=True),
        sa.Column("published_by", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["request_id"], ["feedback_requests.id"], ondelete="SET NULL"),
    )
    op.create_index("idx_feedback_response_created", "feedback_responses", ["created_at"])
    op.create_index("idx_feedback_response_user", "feedback_responses", ["user_id"])


def downgrade() -> None:
    op.drop_table("feedback_responses")
    op.drop_table("feedback_receipts")
    op.drop_index("idx_feedback_request_status", table_name="feedback_requests")
    op.drop_table("feedback_requests")
