"""web_event_teams.color — admin-assigned team accent color.

"#rrggbb" hex string set from the event manager's team controls; NULL means
the frontend keeps its index-based palette default. Reflected everywhere a
team is colored: bingo-board completion dots, hover-card and task-board
progress bars, and team pages.

Revision ID: web40a_event_team_colors
Revises: web39a_user_moderator
"""
from alembic import op
import sqlalchemy as sa

revision = "web40a_event_team_colors"
down_revision = "web39a_user_moderator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_teams",
        sa.Column("color", sa.String(7), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_event_teams", "color")
