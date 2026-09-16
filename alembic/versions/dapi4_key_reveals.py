"""Data API: one-time, audience-bound delivery of a minted key.

Revision ID: dapi4_key_reveals
Revises: dapi3_key_scope
"""
import sqlalchemy as sa
from alembic import op

revision = "dapi4_key_reveals"
down_revision = "dapi3_key_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_key_reveals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("reveal_token", sa.String(64), nullable=False, unique=True),
        sa.Column("api_key_id", sa.BigInteger,
                  sa.ForeignKey("api_keys.id"), nullable=False),
        # Fernet ciphertext; NULLed on view so a later database read cannot
        # recover a secret that has already been delivered.
        sa.Column("secret_ciphertext", sa.Text, nullable=True),
        sa.Column("audience_user_id", sa.Integer, nullable=True),
        sa.Column("audience_group_id", sa.Integer, nullable=True),
        sa.Column("created_by_user_id", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("viewed_at", sa.DateTime, nullable=True),
        sa.Column("viewed_by_user_id", sa.Integer, nullable=True),
    )
    op.create_index("idx_api_key_reveals_key", "api_key_reveals", ["api_key_id"])


def downgrade() -> None:
    op.drop_table("api_key_reveals")
