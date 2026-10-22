"""plugin_admin_sessions (web142a).

Device sessions for administering a group from the RuneLite plugin, minted in
exchange for the group's verification code (web141a). Holds a SHA-256 of each
token, never the token. Read and written by db/plugin_admin_sessions.py.

New table only, so it is safe to apply live. Guarded, so a re-run is harmless.

Revision ID: web142a_plugin_admin_sessions
Revises: web141a_group_verification_codes
"""
from alembic import op
import sqlalchemy as sa

revision = "web142a_plugin_admin_sessions"
down_revision = "web141a_group_verification_codes"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("plugin_admin_sessions"):
        return
    op.create_table(
        "plugin_admin_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_hash", sa.String(100), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.String(16), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_plugin_admin_sessions_token"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_plugin_admin_sessions_group", "plugin_admin_sessions", ["group_id"])
    op.create_index("ix_plugin_admin_sessions_account", "plugin_admin_sessions", ["account_hash"])


def downgrade():
    op.drop_table("plugin_admin_sessions")
