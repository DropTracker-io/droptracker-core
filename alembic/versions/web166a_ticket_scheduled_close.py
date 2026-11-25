"""tickets.scheduled_close_at / scheduled_close_by: close a ticket later (web166a).

Staff can leave a final reply and schedule the ticket to close in N hours
(``/close hours:12`` or the welcome card's "Close in 12h" button). The bot
posts a notice with a "Keep open" button; the opener or any ticket staff can
press it to cancel. Otherwise the 5-minute sweep archives the ticket once
``scheduled_close_at`` passes. Replies do not cancel it on their own.

Purely additive: every existing ticket reads NULL (nothing scheduled).

Revision ID: web166a_ticket_scheduled_close
Revises: web165a_player_music_varps
"""
from alembic import op
import sqlalchemy as sa

revision = "web166a_ticket_scheduled_close"
down_revision = "web165a_player_music_varps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("scheduled_close_at", sa.DateTime(), nullable=True))
    op.add_column("tickets", sa.Column("scheduled_close_by", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "scheduled_close_by")
    op.drop_column("tickets", "scheduled_close_at")
