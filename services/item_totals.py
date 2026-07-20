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

import calendar
import os
from datetime import date, datetime, timedelta

from sqlalchemy import text

from db.models.base import session as _default_session
from utils.redis import RedisClient

LAST_ID_KEY = "item_totals:last_drop_id"

# Periodic re-fold (heals commit-order gaps the additive tailer skips; see
# refold_day). Tunable via env so ops can slow it down without a deploy.
REFOLD_INTERVAL_SEC = int(os.getenv("ROLLUP_REFOLD_INTERVAL_SEC", "3600"))
REFOLD_PACE_SEC = float(os.getenv("ROLLUP_REFOLD_PACE_SEC", "1.0"))
PREV_MONTH_GRACE_DAYS = int(os.getenv("ROLLUP_REFOLD_PREV_MONTH_DAYS", "3"))

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

# One-day ABSOLUTE re-fold, capped at a drop_id ceiling. Recomputes each
# (player,item,hour) bucket from `drops` and OVERWRITES the rollup (VALUES(),
# not col+VALUES()). Same aggregation as scripts/backfill_item_hourly_gaps, but
# with the `d.drop_id <= :max_id` cap that makes it safe to run on the CURRENT
# partition alongside the live additive tailer (see refold_day). An hour never
# spans two days, so every bucket is fully recomputed within its day chunk,
# making the upsert idempotent.
_REFOLD_DAY_SQL = text(
    "INSERT INTO player_item_hourly_totals "
    "  (player_id, item_id, date_hour, `partition`, quantity, total_value, drop_count, last_drop_time) "
    "SELECT d.player_id, d.item_id, "
    "       DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), "
    "       :partition, "
    "       SUM(d.quantity), SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
    "FROM drops d "
    "WHERE d.date_added >= :day_start AND d.date_added < :day_end AND d.drop_id <= :max_id "
    "  AND d.item_id IS NOT NULL AND d.date_added IS NOT NULL "
    "GROUP BY d.player_id, d.item_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H') "
    "ON DUPLICATE KEY UPDATE "
    "  quantity = VALUES(quantity), "
    "  total_value = VALUES(total_value), "
    "  drop_count = VALUES(drop_count), "
    "  last_drop_time = VALUES(last_drop_time)"
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


def _month_days(partition: int):
    """Yield (day_start, day_end) date pairs for every day of a YYYYMM month."""
    year, month = divmod(int(partition), 100)
    n_days = calendar.monthrange(year, month)[1]
    for day_num in range(1, n_days + 1):
        start = date(year, month, day_num)
        yield start, start + timedelta(days=1)


def refold_partitions() -> list[int]:
    """Partitions the periodic re-fold should recompute: the current month,
    plus the previous month during the first PREV_MONTH_GRACE_DAYS days of a new
    month (late month-boundary drops still land against the previous partition
    and can be skipped by the tailer the same way — until it settles and the
    manual gap-filler owns it)."""
    now = datetime.now()
    cur = now.year * 100 + now.month
    parts = [cur]
    if now.day <= PREV_MONTH_GRACE_DAYS:
        y, m = divmod(cur, 100)
        prev = (y - 1) * 100 + 12 if m == 1 else cur - 1
        parts.insert(0, prev)
    return parts


def refold_plan() -> list[tuple[int, date, date]]:
    """Flattened (partition, day_start, day_end) work list for one re-fold pass,
    so the caller can drive it one day at a time (heartbeats/pacing between days)."""
    plan: list[tuple[int, date, date]] = []
    for partition in refold_partitions():
        for day_start, day_end in _month_days(partition):
            plan.append((partition, day_start, day_end))
    return plan


def refold_day(session, partition: int, day_start, day_end, max_drop_id: int) -> int:
    """Absolute-recompute one day of `partition` from `drops`, capped at
    max_drop_id, and OVERWRITE the rollup for those buckets.

    This heals the additive tailer's blind spot: auto-increment ids are reserved
    at INSERT time but rows only become visible at COMMIT, so a drop whose
    transaction commits *after* process_new_drops has advanced its pointer past
    that drop's id is never folded in (worst with big/slow bulk inserts, which
    reserve many low ids and commit late). Re-scanning by date and overwriting
    picks those drops up.

    Capping at max_drop_id (the tailer pointer) is what makes this safe to run
    on the live current partition: it never counts a drop with id > pointer that
    the additive tailer will still fold, so the two never double-count. Returns
    rows affected."""
    s = session or _default_session
    result = s.execute(
        _REFOLD_DAY_SQL,
        {"partition": int(partition), "day_start": day_start,
         "day_end": day_end, "max_id": int(max_drop_id)},
    )
    s.commit()
    return result.rowcount or 0
