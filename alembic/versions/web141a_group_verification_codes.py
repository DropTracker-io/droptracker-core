"""group_verification_codes (web141a).

One row per group holding a keyed hash of its plugin verification code (never
the code), a generation counter and who issued/cleared it. Read and written by
db/group_verification_codes.py. Rows appear lazily, the first time an owner
reveals a code, so there is no backfill.

New table only, so it is safe to apply live. Guarded, so a re-run is harmless.

Revision ID: web141a_group_verification_codes
Revises: web140a_notification_content_footer
"""
from alembic import op
import sqlalchemy as sa

revision = "web141a_group_verification_codes"
down_revision = "web140a_notification_content_footer"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("group_verification_codes"):
        return
    op.create_table(
        "group_verification_codes",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(80), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("issued_via", sa.String(16), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("issued_by_user_id", sa.Integer(), nullable=True),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
        sa.Column("cleared_by_user_id", sa.Integer(), nullable=True),
        sa.Column("cleared_reason", sa.String(32), nullable=True),
        sa.PrimaryKeyConstraint("group_id"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"], ondelete="CASCADE"),
    )


def downgrade():
    op.drop_table("group_verification_codes")
