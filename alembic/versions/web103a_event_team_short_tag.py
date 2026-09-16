"""Short chat tag for event teams (web103a).

The in-game clan-chat team badge needs a label that fits in a chat line beside
a player's name, which the team's real name ("The Sunday Night Regulars") does
not. ``short_tag`` is that label: admin-settable, ≤8 characters.

NULL is the normal state, not an unfinished one — ``services.event_teams
.derive_short_tag`` turns the team's name into a stable tag, so every team has
a badge from the moment it exists and an admin only sets this column to
override the derivation. Storing the derived value instead would make a rename
silently keep the old tag.

Revision ID: web103a_event_team_short_tag
Revises: web102a_support_widget
"""
from alembic import op
import sqlalchemy as sa

revision = "web103a_event_team_short_tag"
down_revision = "web102a_support_widget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_teams", sa.Column("short_tag", sa.String(length=8), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("web_event_teams", "short_tag")
