"""pvp_kills: loot keys (web148a).

With loot keys on in game, a PvP kill drops only bones; the victim's loot goes
into a key that is opened later at the Wilderness loot chest. The kill already
arrives as a ``pvp_kills`` row (bones only). The key's loot arrives when the
key is opened, as RuneLite's ``"Loot Chest"`` loot, and used to be rejected as
an unknown NPC (ticket 498).

- ``kind``: ``kill`` (every existing row) or ``loot_key``, a key whose loot
  could not be matched to one kill. A ``loot_key`` row adds loot to the boards
  but never a kill: the kill it came from already counted.
- ``loot_key_guid`` / ``loot_key_at``: set on a ``kill`` row when a key's loot
  was matched to it and added to its items. The GUID makes a replay a no-op.

Small table (about 1.3k rows at the time); guarded, so a re-run is harmless.

Revision ID: web148a_pvp_loot_keys
Revises: web147a_completion_matched_npc
"""
from alembic import op
import sqlalchemy as sa

revision = "web148a_pvp_loot_keys"
down_revision = "web147a_completion_matched_npc"
branch_labels = None
depends_on = None

TABLE = "pvp_kills"
UNIQUE = "uq_pvp_kills_loot_key_guid"


def _columns() -> set:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade() -> None:
    have = _columns()
    if "kind" not in have:
        op.add_column(TABLE, sa.Column("kind", sa.String(12), nullable=False,
                                       server_default="kill"))
    if "loot_key_guid" not in have:
        op.add_column(TABLE, sa.Column("loot_key_guid", sa.String(255), nullable=True))
        op.create_unique_constraint(UNIQUE, TABLE, ["loot_key_guid"])
    if "loot_key_at" not in have:
        op.add_column(TABLE, sa.Column("loot_key_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    have = _columns()
    if "loot_key_at" in have:
        op.drop_column(TABLE, "loot_key_at")
    if "loot_key_guid" in have:
        op.drop_constraint(UNIQUE, TABLE, type_="unique")
        op.drop_column(TABLE, "loot_key_guid")
    if "kind" in have:
        op.drop_column(TABLE, "kind")
