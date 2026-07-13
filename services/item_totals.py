"""
Incremental maintenance of the `player_item_hourly_totals` rollup table.

The table powers the item pages' receive totals (web_api/routes/items.py),
item search ranking (web_api/routes/search.py) and the "is this tracked"
resolver flag (web_api/routes/resolve.py), but had no in-repo writer -- the
same failure mode player_npc_hourly_totals had before services/npc_totals.py.
This module tails the `drops` table by drop_id and folds new rows into the
rollup, plus offers a full per-partition rebuild for backfills.

State: Redis key `item_totals:last_drop_id` holds the highest drop_id that has
been folded in. The tailer is idempotent per (from_id, to_id] window as long
as the pointer is only advanced after a successful commit.
"""

from datetime import datetime

from sqlalchemy import text

from db.models.base import session as _default_session
from utils.redis import RedisClient

LAST_ID_KEY = "item_totals:last_drop_id"

_UPSERT_WINDOW_SQL = text(
    "INSERT INTO player_item_hourly_totals "
    "  (player_id, item_id, date_hour, `partition`, quantity, total_value, drop_count, last_drop_time) "
    "SELECT d.player_id, d.item_id, "
    "       DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), "
    "       COALESCE(d.`partition`, CAST(DATE_FORMAT(d.date_added, '%Y%m') AS UNSIGNED)), "
    "       SUM(d.quantity), SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
    "FROM drops d "
    "WHERE d.drop_id > :from_id AND d.drop_id <= :to_id "
    "  AND d.item_id IS NOT NULL AND d.date_added IS NOT NULL "
    "GROUP BY d.player_id, d.item_id, "
    "         DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), "
    "         COALESCE(d.`partition`, CAST(DATE_FORMAT(d.date_added, '%Y%m') AS UNSIGNED)) "
    "ON DUPLICATE KEY UPDATE "
    "  quantity = quantity + VALUES(quantity), "
    "  total_value = total_value + VALUES(total_value), "
    "  drop_count = drop_count + VALUES(drop_count), "
    "  last_drop_time = GREATEST(COALESCE(last_drop_time, VALUES(last_drop_time)), VALUES(last_drop_time))"
)

_REBUILD_PARTITION_SQL = text(
    "INSERT INTO player_item_hourly_totals "
    "  (player_id, item_id, date_hour, `partition`, quantity, total_value, drop_count, last_drop_time) "
    "SELECT d.player_id, d.item_id, "
    "       DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), "
    "       :partition, "
    "       SUM(d.quantity), SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
    "FROM drops d "
    "WHERE d.`partition` = :partition AND d.drop_id <= :max_id "
    "  AND d.item_id IS NOT NULL AND d.date_added IS NOT NULL "
    "GROUP BY d.player_id, d.item_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H')"
)


def _redis():
    return RedisClient().client


def get_pointer() -> int | None:
    raw = _redis().get(LAST_ID_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def set_pointer(drop_id: int) -> None:
    _redis().set(LAST_ID_KEY, int(drop_id))


def process_new_drops(session=None, batch_size: int = 100_000, max_batches: int = 50) -> int:
    """Fold drops newer than the stored pointer into the rollup.

    On first run (no pointer) the pointer is initialised to MAX(drop_id) and
    nothing is processed -- history is the backfill's job
    (scripts/backfill_item_hourly_totals.py). Returns rows scanned.
    """
    s = session or _default_session
    max_id = s.execute(text("SELECT COALESCE(MAX(drop_id), 0) FROM drops")).scalar()
    pointer = get_pointer()
    if pointer is None:
        set_pointer(max_id)
        return 0

    scanned = 0
    for _ in range(max_batches):
        if pointer >= max_id:
            break
        to_id = min(pointer + batch_size, max_id)
        s.execute(_UPSERT_WINDOW_SQL, {"from_id": pointer, "to_id": to_id})
        s.commit()
        set_pointer(to_id)
        scanned += to_id - pointer
        pointer = to_id
    return scanned


def rebuild_partition(partition: int, session=None, advance_pointer: bool = True) -> int:
    """Delete + rebuild one YYYYMM partition of the rollup from `drops`.

    Uses a drop_id snapshot ceiling so rows inserted mid-rebuild are left for
    the tailer. With advance_pointer=True (backfill/deploy flow) the pointer
    is moved up to the snapshot ceiling so the tailer doesn't double-count the
    rebuilt window. Returns the number of rollup rows created.
    """
    s = session or _default_session
    max_id = s.execute(text("SELECT COALESCE(MAX(drop_id), 0) FROM drops")).scalar()
    s.execute(
        text("DELETE FROM player_item_hourly_totals WHERE `partition` = :partition"),
        {"partition": int(partition)},
    )
    result = s.execute(_REBUILD_PARTITION_SQL, {"partition": int(partition), "max_id": max_id})
    s.commit()
    if advance_pointer:
        pointer = get_pointer()
        if pointer is None or pointer < max_id:
            set_pointer(max_id)
    return result.rowcount or 0


def current_partition() -> int:
    now = datetime.now()
    return now.year * 100 + now.month
