"""web_events per-clan roster limits for staff-hosted clan-vs-clan (web119a).

A clan_vs_clan event with no host group (group_id NULL) is run by DropTracker
staff: staff own the setup, and each invited clan that accepts fields one team
whose roster its own leaders pick from their members' sign-ups. Staff bound
that roster with a per-clan minimum and maximum; a clan still under the
minimum when the event starts is dropped. The lock flag stops clan leaders
changing their roster once the event is live (staff always can).

Purely additive: three nullable/defaulted columns. The new participant
statuses ("withdrawn", "dropped") need no DDL — web_event_groups.status is a
VARCHAR(16).

Revision ID: web119a_staff_hosted_cvc
Revises: web118a_popup_notices
"""
from alembic import op
import sqlalchemy as sa

revision = "web119a_staff_hosted_cvc"
down_revision = "web118a_popup_notices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_events", sa.Column("clan_roster_min", sa.Integer(), nullable=True))
    op.add_column("web_events", sa.Column("clan_roster_max", sa.Integer(), nullable=True))
    op.add_column(
        "web_events",
        sa.Column("clan_roster_locked_at_start", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
    )


def downgrade() -> None:
    op.drop_column("web_events", "clan_roster_locked_at_start")
    op.drop_column("web_events", "clan_roster_max")
    op.drop_column("web_events", "clan_roster_min")
