"""Suggestion forum: two-way Discord mirror + reply threads.

Suggestions become full threads mirrored 1:1 with Discord forum posts:
``suggestion_messages`` holds replies from either side, and ``suggestions``
gains origin/author-snapshot/activity metadata so Discord-native posts (whose
authors may have no site account) can be listed and rendered on the web.
``user_id`` therefore becomes nullable.
"""

from alembic import op
import sqlalchemy as sa


revision = "web29a_suggestion_forum"
down_revision = "web28a_suggestions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "suggestions",
        "user_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.add_column(
        "suggestions",
        sa.Column("origin", sa.String(length=8), nullable=False, server_default="web"),
    )
    op.add_column("suggestions", sa.Column("author_discord_id", sa.String(length=35), nullable=True))
    op.add_column("suggestions", sa.Column("author_name", sa.String(length=100), nullable=True))
    op.add_column(
        "suggestions",
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "suggestions",
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "suggestions",
        sa.Column("last_activity_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_suggestion_activity", "suggestions", ["last_activity_at"])
    # Existing rows: activity = creation time, not the migration timestamp.
    op.get_bind().execute(sa.text("UPDATE suggestions SET last_activity_at = created_at"))

    op.create_table(
        "suggestion_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "suggestion_id",
            sa.Integer(),
            sa.ForeignKey("suggestions.id"),
            nullable=False,
        ),
        sa.Column("author_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("author_discord_id", sa.String(length=35), nullable=True),
        sa.Column("author_name", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("discord_message_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("edited_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("discord_message_id", name="uix_suggestion_message_discord"),
    )
    op.create_index(
        "idx_suggestion_message_thread", "suggestion_messages", ["suggestion_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_suggestion_message_thread", table_name="suggestion_messages")
    op.drop_table("suggestion_messages")
    op.drop_index("idx_suggestion_activity", table_name="suggestions")
    op.drop_column("suggestions", "last_activity_at")
    op.drop_column("suggestions", "message_count")
    op.drop_column("suggestions", "is_open")
    op.drop_column("suggestions", "author_name")
    op.drop_column("suggestions", "author_discord_id")
    op.drop_column("suggestions", "origin")
    op.alter_column(
        "suggestions",
        "user_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
