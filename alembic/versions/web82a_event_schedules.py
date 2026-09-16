"""Recurring event activation schedules (web82a).

Two additive pieces:

- ``web_events.schedule_config`` — JSON rule describing when, inside the
  event's overall span, scoring is open (weekly day/time spans, interval
  patterns, daily time-of-day, or a custom window list). NULL = continuous.
- ``web_event_windows`` — the rule compiled into explicit ``[start, end)``
  scoring windows (services/event_schedule.py). Every read-side consumer
  (scoring gate, WOM reconciler, loot totals, displays) reads these rows.

FK cascades on delete so removing an event takes its windows with it (same
pattern as the other web_event_* children).

Revision ID: web82a_event_schedules
Revises: web81a_dev_tracker
"""
from alembic import op
import sqlalchemy as sa

revision = "web82a_event_schedules"
down_revision = "web81a_dev_tracker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_events", sa.Column("schedule_config", sa.Text(), nullable=True))
    op.create_table(
        "web_event_windows",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("event_id", sa.Integer(),
                  sa.ForeignKey("web_events.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="rule"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_web_event_window_event", "web_event_windows",
                    ["event_id", "starts_at"])


def downgrade() -> None:
    op.drop_index("idx_web_event_window_event", table_name="web_event_windows")
    op.drop_table("web_event_windows")
    op.drop_column("web_events", "schedule_config")
