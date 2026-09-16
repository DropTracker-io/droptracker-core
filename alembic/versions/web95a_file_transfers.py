"""Direct-URL file transfers (web95a).

Two tables behind the unlisted ``/file-transfer`` page and its admin CP tab.

``file_transfers`` is the envelope (owner, label, expiry); ``file_transfer_versions``
holds every uploaded revision — v1 from the user, later versions from staff
replying with an updated copy. Split rather than one table with a self-FK
because "download any version" is the headline requirement: versions are the
rows that get listed, and the envelope is what gets pruned.

The bytes live in B2 under ``dt_transfers/``; only the object key is stored.

Revision ID: web95a_file_transfers
Revises: web94a_clan_log
"""
from alembic import op
import sqlalchemy as sa

revision = "web95a_file_transfers"
down_revision = "web94a_clan_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_transfers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("latest_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        # No server_default: every row's expiry is computed from its newest
        # version at write time (created_at + 30d, refreshed on each version).
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
    )
    op.create_index("idx_file_transfer_owner", "file_transfers",
                    ["owner_user_id", "created_at"])
    op.create_index("idx_file_transfer_expires", "file_transfers", ["expires_at"])

    op.create_table(
        "file_transfer_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("uploaded_by_user_id", sa.Integer(), nullable=False),
        sa.Column("uploaded_by_role", sa.String(8), nullable=False,
                  server_default="user"),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False,
                  server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["transfer_id"], ["file_transfers.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.user_id"]),
        # One object key per row — makes the pruner's delete-then-drop idempotent.
        sa.UniqueConstraint("storage_key", name="uq_file_transfer_version_key"),
        sa.UniqueConstraint("transfer_id", "version", name="uq_file_transfer_version"),
    )
    op.create_index("idx_file_transfer_version_transfer", "file_transfer_versions",
                    ["transfer_id", "version"])


def downgrade() -> None:
    op.drop_index("idx_file_transfer_version_transfer",
                  table_name="file_transfer_versions")
    op.drop_table("file_transfer_versions")
    op.drop_index("idx_file_transfer_expires", table_name="file_transfers")
    op.drop_index("idx_file_transfer_owner", table_name="file_transfers")
    op.drop_table("file_transfers")
