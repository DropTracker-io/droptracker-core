"""Sign-ups close when the event starts (web70a).

- ``web_events.allow_late_signups`` — opt-in per-event boolean. OFF (the
  default, for every existing and new event) means self sign-ups stop the
  moment the event begins; ON keeps the old behaviour (players may still
  enter mid-event, right up to the end).
- ``web_event_signup_messages`` — the Discord sign-up prompts the bot has
  posted, so it can go back and retire them (drop the "Sign up" button and
  re-render as closed) once sign-ups end. Written by the notification
  service on send, closed out by the core bot's retire sweep. Nothing
  recorded the message id before, which is exactly why a posted prompt kept
  offering its button forever.

Revision ID: web70a_event_signup_close
Revises: web69a_task_target_value_bigint
"""
from alembic import op
import sqlalchemy as sa

revision = "web70a_event_signup_close"
down_revision = "web69a_task_target_value_bigint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column("allow_late_signups", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_table(
        "web_event_signup_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("channel_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(32), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_web_evt_signup_msg", "web_event_signup_messages",
        ["event_id", "message_id"], unique=True,
    )
    # The retire sweep's whole query: un-closed rows, newest event first.
    op.create_index(
        "idx_web_evt_signup_msg_open", "web_event_signup_messages",
        ["closed_at", "event_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_web_evt_signup_msg_open", table_name="web_event_signup_messages")
    op.drop_index("uq_web_evt_signup_msg", table_name="web_event_signup_messages")
    op.drop_table("web_event_signup_messages")
    op.drop_column("web_events", "allow_late_signups")
