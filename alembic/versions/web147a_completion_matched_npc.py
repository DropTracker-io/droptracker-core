"""web_event_completions.matched_npc: the NPC a loot_sweep receipt dropped from (web147a).

A Loot Sweep item may now repeat across groups of one set when the groups name
different NPCs (Dragon 2h under both Chaos Elemental and Venenatis in a
"Wilderness Bosses" set). The ledger row used to carry only the item name, so
two groups holding the same name could not be told apart. The engine now
stores the drop's NPC here and services/loot_sweep.py routes the receipt to
the group that lists that NPC.

NULL on older rows, pets, manual awards that name no boss and every other task
type. No backfill is needed: before this, a name could only sit in one group
per set, so an older row's group is always unambiguous.

Nullable column at the end of the table: an instant ALTER on MariaDB, safe to
apply live. Guarded, so a re-run is harmless.

Revision ID: web147a_completion_matched_npc
Revises: web146a_community_listings
"""
from alembic import op
import sqlalchemy as sa

revision = "web147a_completion_matched_npc"
down_revision = "web146a_community_listings"
branch_labels = None
depends_on = None

TABLE = "web_event_completions"
COLUMN = "matched_npc"


def _has_column() -> bool:
    insp = sa.inspect(op.get_bind())
    return any(c["name"] == COLUMN for c in insp.get_columns(TABLE))


def upgrade() -> None:
    if not _has_column():
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(64), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column(TABLE, COLUMN)
