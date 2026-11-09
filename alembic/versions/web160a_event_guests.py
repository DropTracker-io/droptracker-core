"""web_event_guests (web160a).

Players a manager put in an event although they are in none of its clans.
Only a manager's manual add (team add, team paste, sign-up pool add) makes a
row; self sign-up stays members-only. The row keeps a guest in the event: the
clan roster sync leaves them alone, and on a clan_vs_clan event they may sit
on a team of a clan they are not in. New table, no backfill.

Revision ID: web160a_event_guests
Revises: web159a_bank_holders
"""
from alembic import op
import sqlalchemy as sa

revision = "web160a_event_guests"
down_revision = "web159a_bank_holders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "web_event_guests" in inspector.get_table_names():
        return
    op.create_table(
        "web_event_guests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(),
                  sa.ForeignKey("web_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("player_id", sa.Integer(),
                  sa.ForeignKey("players.player_id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_by", sa.Integer(),
                  sa.ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("uq_web_event_guest", "web_event_guests",
                    ["event_id", "player_id"], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "web_event_guests" in inspector.get_table_names():
        op.drop_table("web_event_guests")
