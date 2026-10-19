"""drops: drop the redundant player_id index; adopt idx_item_stats_covering (data02).

1. ``drops`` carries a single-column index on ``player_id`` next to the
   composite ``ix_drops_player_id_date_added`` (data01), whose leading column is
   the same ``player_id``. Every lookup the single-column index can serve, the
   composite serves too, and it also backs the ``player_id`` foreign key. The
   redundant one only costs a B-tree write on every drop insert (~200M rows)
   and lets the planner build the slow ``player_id`` x ``partition`` index
   merge that profiles.py, recap.py and group_export.py had to force their way
   around. No query hints name it.

   ``ix_drops_partition`` is deliberately kept: rebuild_partition, the Redis
   partition stream and several moderation/split reads still filter on
   ``partition =`` and would lose their access path.

   Dropping a secondary index is metadata-only in InnoDB, but the ALTER still
   needs a brief exclusive metadata lock. ``lock_wait_timeout`` is lowered for
   it so a long-running reader (or the nightly dump, 08:30-10:00 UTC -- don't
   run migrations then) makes the ALTER fail fast instead of queueing every
   drop insert behind it. Re-run later if it times out.

2. ``idx_item_stats_covering`` on ``player_item_hourly_totals`` exists on the
   production database only; web_api/routes/items.py depends on it for
   index-only item stats. This records it (and the model declares it) so a
   fresh or restored schema gets it too. A no-op where it already exists.

Revision ID: data02_drops_index_cleanup
Revises: web136a_pvp_kills
"""
from alembic import op
import sqlalchemy as sa

revision = "data02_drops_index_cleanup"
down_revision = "web136a_pvp_kills"
branch_labels = None
depends_on = None

COMPOSITE_PREFIX = ["player_id", "date_added"]
PLAYER_INDEX_NAME = "ix_drops_player_id"
COVERING_INDEX_NAME = "idx_item_stats_covering"
COVERING_COLUMNS = (
    "item_id, player_id, `partition`, total_value, quantity, drop_count, last_drop_time"
)
# Seconds to wait for the metadata lock before giving up on the ALTER.
DDL_LOCK_WAIT_SECONDS = 10


def _indexes(conn, table):
    return sa.inspect(conn).get_indexes(table)


def _redundant_player_indexes(conn) -> list:
    """Names of single-column, non-unique player_id indexes on drops -- only
    when the (player_id, date_added) composite is there to take over."""
    indexes = _indexes(conn, "drops")
    has_composite = any(ix["column_names"][:2] == COMPOSITE_PREFIX for ix in indexes)
    if not has_composite:
        return []
    return [
        ix["name"]
        for ix in indexes
        if ix["column_names"] == ["player_id"] and not ix.get("unique")
    ]


def upgrade() -> None:
    conn = op.get_bind()

    redundant = _redundant_player_indexes(conn)
    if redundant:
        previous = conn.execute(sa.text("SELECT @@SESSION.lock_wait_timeout")).scalar()
        conn.execute(sa.text(f"SET SESSION lock_wait_timeout = {DDL_LOCK_WAIT_SECONDS}"))
        try:
            for name in redundant:
                op.execute(
                    f"ALTER TABLE drops DROP INDEX `{name}`, ALGORITHM=INPLACE, LOCK=NONE"
                )
        finally:
            conn.execute(sa.text(f"SET SESSION lock_wait_timeout = {int(previous)}"))

    existing = {ix["name"] for ix in _indexes(conn, "player_item_hourly_totals")}
    if COVERING_INDEX_NAME not in existing:
        op.execute(
            f"ALTER TABLE player_item_hourly_totals ADD INDEX {COVERING_INDEX_NAME} "
            f"({COVERING_COLUMNS}), ALGORITHM=INPLACE, LOCK=NONE"
        )


def downgrade() -> None:
    conn = op.get_bind()
    # Restores the player_id index (an online build over every drop row; hours
    # on production). The covering index is left alone: production had it
    # before this revision recorded it.
    indexes = _indexes(conn, "drops")
    if not any(ix["column_names"] == ["player_id"] for ix in indexes):
        op.execute(
            f"ALTER TABLE drops ADD INDEX {PLAYER_INDEX_NAME} (player_id), "
            "ALGORITHM=INPLACE, LOCK=NONE"
        )
