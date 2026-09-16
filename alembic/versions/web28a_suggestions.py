"""Suggestions & bug reports: web form → Discord forum syndication.

Adds the ``suggestions`` table backing the website's /suggestions page. The
Web API inserts rows and enqueues a ``kind='forum_post'`` discord_outbox row;
the core bot creates the post in the matching forum channel and writes back
``discord_thread_id`` (see services/discord_outbox.py).
"""

from alembic import op
import sqlalchemy as sa


revision = "web28a_suggestions"
down_revision = "web27a_tier_flair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suggestions",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=16), nullable=False, server_default="suggestion"),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("discord_thread_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_suggestion_user_created", "suggestions", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_suggestion_user_created", table_name="suggestions")
    op.drop_table("suggestions")
