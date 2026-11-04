"""web_events.partial_points (web156a).

When on, a "points from a list" task a team has not finished by the end of
the event pays the same share of its task points as the share of the points
goal the team reached, rounded down (services/event_partial_points.py). Off
for every existing event, so nothing changes until someone flips it.

Revision ID: web156a_event_partial_points
Revises: web155a_feedback
"""
from alembic import op
import sqlalchemy as sa

revision = "web156a_event_partial_points"
down_revision = "web155a_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column("partial_points", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("web_events", "partial_points")
