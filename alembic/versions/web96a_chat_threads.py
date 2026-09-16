"""Generic chat threads + DM-capable outbox (web96a).

Four tables behind ``db/models/chat.py`` and ``services/chat.py``: threads
anchored to an arbitrary ``(subject_type, subject_id)``, party-based
participants, a message timeline that carries both human and system entries,
and a per-user read pointer.

Also widens ``discord_outbox`` so the Web API can queue a DIRECT MESSAGE rather
than only a channel post: ``kind='dm'`` reuses ``channel_id`` to hold the
recipient's Discord *user* id, and the new ``components_json`` carries link
buttons (URL buttons raise no interaction, so the bot needs no new handler).
That is what puts an actionable "you've been challenged" card in a clan
leader's DMs.

Revision ID: web96a_chat_threads
Revises: web95a_file_transfers
"""
from alembic import op
import sqlalchemy as sa

revision = "web96a_chat_threads"
down_revision = "web95a_file_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_threads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.user_id"]),
        # Race-safe get-or-create: concurrent bulk invites of the same clan
        # collide here instead of creating two threads for one negotiation.
        sa.UniqueConstraint("kind", "subject_type", "subject_id",
                            name="uq_chat_thread_subject"),
    )
    op.create_index("idx_chat_thread_activity", "chat_threads",
                    ["status", "last_message_at"])

    op.create_table(
        "chat_participants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("thread_id", sa.Integer(), nullable=False),
        sa.Column("party_type", sa.String(16), nullable=False),
        # Deliberately not a FK: the referent depends on party_type
        # (groups.group_id or users.user_id) and MySQL has no polymorphic FK.
        sa.Column("party_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("added_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["thread_id"], ["chat_threads.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("thread_id", "party_type", "party_id",
                            name="uq_chat_participant"),
    )
    op.create_index("idx_chat_participant_party", "chat_participants",
                    ["party_type", "party_id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("thread_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="message"),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("author_party_type", sa.String(16), nullable=True),
        sa.Column("author_party_id", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("attachments_json", sa.Text(), nullable=True),
        sa.Column("system_code", sa.String(32), nullable=True),
        sa.Column("system_data_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by_user_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["thread_id"], ["chat_threads.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["deleted_by_user_id"], ["users.user_id"]),
    )
    op.create_index("idx_chat_message_thread", "chat_messages",
                    ["thread_id", "id"])

    op.create_table(
        "chat_reads",
        sa.Column("thread_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("last_read_message_id", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("thread_id", "user_id"),
        sa.ForeignKeyConstraint(["thread_id"], ["chat_threads.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
    )

    # DM support for the outbox. Nullable + no default: existing channel rows
    # are unaffected and the drain treats NULL as "no components".
    op.add_column(
        "discord_outbox",
        sa.Column("components_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discord_outbox", "components_json")
    op.drop_table("chat_reads")
    op.drop_index("idx_chat_message_thread", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("idx_chat_participant_party", table_name="chat_participants")
    op.drop_table("chat_participants")
    op.drop_index("idx_chat_thread_activity", table_name="chat_threads")
    op.drop_table("chat_threads")
