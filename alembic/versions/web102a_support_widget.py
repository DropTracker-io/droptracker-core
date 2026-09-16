"""Site chat widget: unified inbox read state + Discord-bridge columns (web102a).

Four independent pieces behind one feature — the bottom-right support/chat
popup on the site:

* ``chat_messages`` gains ``source`` + ``discord_message_id``. Every other
  two-way mirror in the codebase (ticket_messages, suggestion_messages) relies
  on a unique Discord message id for idempotency and a source marker for echo
  prevention; the chat layer grows the same pair so ``staff_dm`` threads can be
  bridged to Discord DMs (services/staff_dm_bridge.py).
* ``surface_reads`` — per-user read pointer for surfaces that live OUTSIDE
  chat_threads (tickets, suggestions). Chat kinds keep ``chat_reads``. No
  backfill: unread is computed against ``max(pointer, own latest message id)``
  (services/inbox.py), so historic threads where the user spoke last read as
  caught-up from day one.
* ``group_notices`` — one row per (group, problem code), mapping to a
  long-lived ``group_notice`` chat thread. The bot raises these when a clan's
  config is broken (services/group_notices.py); recurrences bump the row
  instead of re-notifying.
* ``tickets.channel_id`` becomes nullable and ``ticket_messages`` gains
  ``origin`` — web-created tickets exist as ``status='pending'`` rows before
  the webhook bot provisions their Discord channel.

Revision ID: web102a_support_widget
Revises: web101a_notification_blacklist
"""
from alembic import op
import sqlalchemy as sa

revision = "web102a_support_widget"
down_revision = "web101a_notification_blacklist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="web"),
    )
    op.add_column(
        "chat_messages",
        sa.Column("discord_message_id", sa.String(length=32), nullable=True),
    )
    op.create_unique_constraint(
        "uq_chat_message_discord_id", "chat_messages", ["discord_message_id"]
    )

    op.create_table(
        "surface_reads",
        sa.Column("surface", sa.String(length=16), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "last_read_message_id", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("surface", "ref_id", "user_id"),
    )

    op.create_table(
        "group_notices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="major"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("thread_id", sa.Integer(), nullable=True),
        sa.Column(
            "first_raised_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_raised_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("raise_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("data_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["thread_id"], ["chat_threads.id"]),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "code", name="uq_group_notice"),
    )
    op.create_index(
        "idx_group_notice_status", "group_notices", ["status", "last_raised_at"]
    )

    op.alter_column(
        "tickets",
        "channel_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.add_column(
        "ticket_messages",
        sa.Column(
            "origin", sa.String(length=8), nullable=False, server_default="discord"
        ),
    )


def downgrade() -> None:
    op.drop_column("ticket_messages", "origin")
    op.alter_column(
        "tickets",
        "channel_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_index("idx_group_notice_status", table_name="group_notices")
    op.drop_table("group_notices")
    op.drop_table("surface_reads")
    op.drop_constraint("uq_chat_message_discord_id", "chat_messages", type_="unique")
    op.drop_column("chat_messages", "discord_message_id")
    op.drop_column("chat_messages", "source")
