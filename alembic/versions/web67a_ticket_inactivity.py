"""tickets.inactivity_warned_at — inactivity auto-close (web67a).

Tickets idle for 5 days get a warning that pings both parties; if nobody
replies within the following 24 hours the ticket is archived + closed
automatically. This column marks a ticket as being inside that 24h grace
window (set when the warning posts, cleared on the next human reply). NULL =
not currently warned.

Revision ID: web67a_ticket_inactivity
Revises: web66a_event_layout_overrides
"""
from alembic import op
import sqlalchemy as sa

revision = "web67a_ticket_inactivity"
down_revision = "web66a_event_layout_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("inactivity_warned_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "inactivity_warned_at")
