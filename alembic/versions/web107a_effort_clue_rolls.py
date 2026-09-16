"""web_event_effort.rolls — clue scrolls a player was actually dealt in-window

A casket is the only "kill" in the game a player can bank. Clue tiers were
therefore excluded from EHE outright: a stack of 100 elites saved up before the
event and dumped in the first five minutes would have bought a hundred clues'
worth of hours it never cost (suggestion #156, joel). Prod bears the fear out —
between 47% and 76% of consecutive casket openings per player land less than
30 seconds apart.

Fazebook's two-part check, which this column makes possible: count the clues
ROLLED inside the event window as well as the caskets OPENED inside it, and pay
for ``min(rolled, opened)`` per tier. Its own column rather than ``kills`` or
``completions`` because it counts a third thing — a scroll arriving from some
*other* NPC's drop table, which is neither an attempt at the clue nor a
completion of one.

See services/event_effort.CLUE_TIERS.

Revision ID: web107a_effort_clue_rolls
Revises: web106a_death_detail_columns
"""
from alembic import op
import sqlalchemy as sa

revision = "web107a_effort_clue_rolls"
down_revision = "web106a_death_detail_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "web_event_effort",
        sa.Column("rolls", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("web_event_effort", "rolls")
