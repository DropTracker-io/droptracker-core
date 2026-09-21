"""tickets.autoclose_exempt — staff opt-out of the inactivity auto-close (web117a).

Some tickets are meant to stay open for a long time: a feature being built, an
API token being set up, a slow investigation. Staff run ``/autoclose`` in the
ticket channel to exempt one, and the 5-day inactivity sweep then skips it
entirely: no warning, no auto-close. It stays open until someone closes it.

Purely additive: every existing ticket reads 0 (not exempt), so the sweep
behaves exactly as before until staff mark one.

Revision ID: web117a_ticket_autoclose_exempt
Revises: web116a_member_death_messages
"""
from alembic import op
import sqlalchemy as sa

revision = "web117a_ticket_autoclose_exempt"
down_revision = "web116a_member_death_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("autoclose_exempt", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("tickets", "autoclose_exempt")
