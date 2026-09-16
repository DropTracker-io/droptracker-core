"""Event Discord messaging overhaul: verbosity config, live board, layouts.

- web_events.message_config — JSON verbosity knobs a group leader edits on
  the event's Discord settings page ({"toggles": {queue type: bool},
  "task_progress": off|milestones|all, "leaderboard": {live, top_n,
  show_tasks}}). NULL = defaults; merge semantics in
  services/event_notifications.py effective_message_config().
- web_event_channels.message_id / message_updated_at — the persistent bot
  message a channel kind owns (the 'leaderboard' kind's live standings
  board, kept edited in place by services/event_board.py — the lootboard
  pattern applied to events).
- web_event_message_layouts — Components-V2 layout templates per
  (group, message type), the component analogue of group_embeds: group 1
  rows are the seeded system defaults
  (scripts/seed_event_message_layouts.py), other groups override with the
  custom_embeds entitlement. Rendered by services/event_message_layouts.py.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision = "web41a_event_messages"
down_revision = "web40a_event_team_colors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_events", sa.Column("message_config", sa.Text(), nullable=True))
    op.add_column(
        "web_event_channels",
        sa.Column("message_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "web_event_channels",
        sa.Column("message_updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "web_event_message_layouts",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.group_id"),
            nullable=False,
            server_default="1",
        ),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("accent_color", sa.String(length=7), nullable=True),
        sa.Column(
            "layout",
            sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "uq_web_event_msg_layout",
        "web_event_message_layouts",
        ["group_id", "message_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_web_event_msg_layout", table_name="web_event_message_layouts")
    op.drop_table("web_event_message_layouts")
    op.drop_column("web_event_channels", "message_updated_at")
    op.drop_column("web_event_channels", "message_id")
    op.drop_column("web_events", "message_config")
