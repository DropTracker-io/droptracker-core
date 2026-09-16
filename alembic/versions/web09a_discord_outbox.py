"""Discord outbox queue (backend Task 09 / 12).

A queue of Discord messages for the bot to send — announcement syndication
(§10.1/§10.2) and the superadmin message sender (§14.1). The Web API inserts
rows; the Discord bot drains them (``services/discord_outbox.py``). Keeps the
"processors never open a Discord connection from the Web API" rule intact.
"""

from alembic import op
import sqlalchemy as sa


revision = "web09a_outbox"
down_revision = "web08a_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discord_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False, server_default="message"),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("embed_json", sa.Text(), nullable=True),
        sa.Column("ref_type", sa.String(length=24), nullable=True),
        sa.Column("ref_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("discord_message_id", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_outbox_status_created", "discord_outbox", ["status", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_outbox_status_created", table_name="discord_outbox")
    op.drop_table("discord_outbox")
