"""admin_widget_tokens (web128a).

Device tokens for the owner's Android admin widget: one row per paired phone,
holding a SHA-256 of the token (never the token), a label, and usage/revocation
times. Read by web_api/routes/admin_widget.py.

New table only, so it is safe to apply live. Guarded, so a re-run is harmless.

Revision ID: web128a_admin_widget_tokens
Revises: web127a_pb_precise_timing
"""
from alembic import op
import sqlalchemy as sa

revision = "web128a_admin_widget_tokens"
down_revision = "web127a_pb_precise_timing"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("admin_widget_tokens"):
        return
    op.create_table(
        "admin_widget_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_hint", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
    )
    op.create_index("ux_awt_token_hash", "admin_widget_tokens", ["token_hash"], unique=True)
    op.create_index("idx_awt_user", "admin_widget_tokens", ["user_id"])


def downgrade():
    op.drop_table("admin_widget_tokens")
