"""Per-event board/task visibility.

Some events are meant to be played blind: the organisers know the tasks, the
players find out what counted when it does. Others just want the board kept
back until the reveal. ``web_events.tasks_visibility`` covers both — "public"
(today's behaviour, and the default) or "admins", which keeps the task list,
the bingo cells, the board-game tiles and the board images to event admins
and event managers. Scoring is unaffected; only what a participant is shown.

Additive, with a server default, so nothing existing changes.

Revision ID: web112a_event_tasks_visibility
Revises: web111a_ca_points_source
"""
from alembic import op
import sqlalchemy as sa

revision = "web112a_event_tasks_visibility"
down_revision = "web111a_ca_points_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column(
            "tasks_visibility",
            sa.String(length=16),
            nullable=False,
            server_default="public",
        ),
    )


def downgrade() -> None:
    op.drop_column("web_events", "tasks_visibility")
