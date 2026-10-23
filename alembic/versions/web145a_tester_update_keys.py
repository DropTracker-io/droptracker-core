"""tester_update_keys (web145a).

The keys tester client builds update themselves with: each website download
writes a new one into its copy of the zip. Holds a SHA-256 of each key, never
the key. Read and written by web_api/routes/tester_builds.py.

New table only, so it is safe to apply live. Guarded, so a re-run is harmless.

Revision ID: web145a_tester_update_keys
Revises: web144a_bank_coffer_sync
"""
from alembic import op
import sqlalchemy as sa

revision = "web145a_tester_update_keys"
down_revision = "web144a_bank_coffer_sync"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tester_update_keys"):
        return
    op.create_table(
        "tester_update_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("file_name", sa.String(160), nullable=True),
        sa.Column("build_id", sa.String(96), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_tester_update_keys_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
    )
    op.create_index("idx_tuk_user_created", "tester_update_keys", ["user_id", "created_at"])


def downgrade():
    op.drop_table("tester_update_keys")
