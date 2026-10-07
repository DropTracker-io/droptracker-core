"""player_last_seen (web132a).

``player_last_seen``  when each player's DropTracker plugin last talked to us:
                      any plugin submission (either transport) or an event
                      notifications poll. Written by utils/player_last_seen.py
                      at most once per account per few minutes, read by the
                      data API's ``identity.last_seen``.

A new table only, so it is safe to apply live. Guarded, so a re-run is
harmless. Seed it with scripts/backfill_player_last_seen.py.

Revision ID: web132a_player_last_seen
Revises: web131a_group_bank
"""
from alembic import op
import sqlalchemy as sa

revision = "web132a_player_last_seen"
down_revision = "web131a_group_bank"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("player_last_seen"):
        return
    op.create_table(
        "player_last_seen",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("player_id"),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"], ondelete="CASCADE"),
    )


def downgrade():
    op.drop_table("player_last_seen")
