"""web_event_teams.auto_clan — whole-clan fallback teams for clan-vs-clan.

When a clan-vs-clan event is activated with no teams set up, the lifecycle
auto-creates one team per accepted clan flagged ``auto_clan=1``. Such a team
represents the entire clan: the matcher credits every current member of the
represented clan to it (no explicit roster rows), so it runs as "anyone in
clan A vs anyone in clan B". Explicit teams keep ``auto_clan=0``.

Revision ID: web42a_event_team_auto_clan
Revises: web41a_event_messages
"""
from alembic import op
import sqlalchemy as sa

revision = "web42a_event_team_auto_clan"
down_revision = "web41a_event_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_teams",
        sa.Column(
            "auto_clan", sa.Boolean(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("web_event_teams", "auto_clan")
