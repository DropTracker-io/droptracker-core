"""tickets.pings_muted: turn off a ticket's per-message pings (web149a).

Every human message in a ticket ghost-pings the other side: staff replies ping
the opener, the opener's messages ping the support role. Some tickets don't
want that (a long back-and-forth, an opener who is watching the channel
anyway). Anyone in the ticket can mute it with ``/pings`` or the welcome
card's button, and the bot then sends no pings either way until it's unmuted.

Purely additive: every existing ticket reads 0 (pings on), so nothing changes
until someone mutes one.

Revision ID: web149a_ticket_pings_muted
Revises: web148a_pvp_loot_keys
"""
from alembic import op
import sqlalchemy as sa

revision = "web149a_ticket_pings_muted"
down_revision = "web148a_pvp_loot_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("pings_muted", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("tickets", "pings_muted")
