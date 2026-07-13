"""Custom-timeframe lootboard data sources.

Two tiers, selected by ``classify_range``:

**Tier 1 — Redis daily hashes** (fast path): the drop-ingest path maintains
``player:{id}:daily:{YYYYMMDD}:total_items`` hashes (90-day TTL, see
``services/redis_updates``). Any day-aligned range inside that retention can be
served by merging the members' daily hashes — measured at ~4ms for a
31-member × 7-day window vs ~99s for the equivalent ``drops``-table scan.

**Tier 2 — hourly rollup** (``player_item_hourly_totals``): maintained live by
``services/item_totals`` (60s tailer) and backfilled per-partition by
``scripts/backfill_item_hourly_totals.py``. Serves ranges older than the Redis
retention at hour precision via ``idx_player_date_hour``. Coverage is not yet
complete (the backfill runs partition-by-partition), so callers must check
``missing_rollup_partitions`` first and refuse gracefully. Note two known
deltas vs tier 1: the rollup does not exclude hidden drops, and it has no
recent-drops feed (the board's recent panel renders empty for old ranges).

Pure helpers are kept free of I/O for unit testing; the fetchers take their
connections explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

# Keep in sync with services.redis_updates.RedisLootTracker._DAILY_TTL —
# resolved lazily in effective_retention_days() to avoid import cycles.
DEFAULT_RETENTION_DAYS = 90

# Public image host serving /store/droptracker/disc/static/assets/img.
IMG_BASE = "https://www.droptracker.io/img"
_IMG_FS_PREFIX = "/store/droptracker/disc/static/assets/img"

# The rollup's first populated month (drops history starts 2024-10).
ROLLUP_EPOCH = date(2024, 10, 1)


def effective_retention_days() -> int:
    try:
        from services.redis_updates import RedisLootTracker

        return int(RedisLootTracker._DAILY_TTL // 86400)
    except Exception:
        return DEFAULT_RETENTION_DAYS


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def daily_tokens(start_day: date, end_day: date) -> List[str]:
    """YYYYMMDD tokens for every day from start_day to end_day inclusive."""
    if end_day < start_day:
        return []
    days: List[str] = []
    cursor = start_day
    while cursor <= end_day:
        days.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return days


def month_partitions(start_day: date, end_day: date) -> List[int]:
    """YYYYMM partition ints touched by the inclusive day range, ascending."""
    if end_day < start_day:
        return []
    parts: List[int] = []
    cursor = date(start_day.year, start_day.month, 1)
    while cursor <= end_day:
        parts.append(cursor.year * 100 + cursor.month)
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return parts


def parse_item_hash_value(raw) -> Optional[Tuple[int, int]]:
    """(quantity, total_value) from a total_items hash value.

    Stored format is ``qty,value[,drop_count,first_ts,last_ts]``; numbers may
    be float-formatted (the Lua writer round-trips through tostring, and other
    paths use INCRBYFLOAT), so accept ``1.5e+07`` style values.
    """
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    parts = str(raw).split(",")
    if len(parts) < 2:
        return None
    try:
        return int(float(parts[0])), int(float(parts[1]))
    except (TypeError, ValueError):
        return None


def merge_item_hashes(hashes: Iterable[dict]) -> Dict[str, Tuple[int, int]]:
    """Fold many total_items hashes into ``{item_id: (qty, value)}``."""
    merged: Dict[str, Tuple[int, int]] = {}
    for h in hashes:
        if not h:
            continue
        for key, value in h.items():
            item_id = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            parsed = parse_item_hash_value(value)
            if parsed is None:
                continue
            qty, val = parsed
            prev_qty, prev_val = merged.get(item_id, (0, 0))
            merged[item_id] = (prev_qty + qty, prev_val + val)
    return merged


def format_group_items(merged: Dict[str, Tuple[int, int]]) -> Dict[str, str]:
    """Renderer wire format: ``{item_id: "qty,value"}`` (drops zero-value rows)."""
    return {
        item_id: f"{qty},{val}"
        for item_id, (qty, val) in merged.items()
        if qty > 0 or val > 0
    }


@dataclass
class RangePlan:
    """How a requested inclusive day range will be served."""
    mode: str                 # "redis" | "hourly"
    start_day: date
    end_day: date


def classify_range(start_day: date, end_day: date, today: Optional[date] = None,
                   retention_days: Optional[int] = None) -> RangePlan:
    """Pick the serving tier for an inclusive [start_day, end_day] range.

    Raises ValueError with a user-presentable message for impossible ranges.
    """
    today = today or date.today()
    retention = retention_days if retention_days is not None else effective_retention_days()
    if end_day < start_day:
        raise ValueError("End date must be on or after the start date.")
    if end_day > today:
        raise ValueError("End date cannot be in the future.")
    if start_day < ROLLUP_EPOCH:
        raise ValueError(f"No loot data exists before {ROLLUP_EPOCH.isoformat()}.")
    if (end_day - start_day).days > 366:
        raise ValueError("Ranges longer than a year are not supported.")
    # The daily hash for a given day only survives ~retention days past its
    # creation; keep a one-day safety margin so a mid-generation expiry can't
    # produce a silently partial board.
    oldest_safe = today - timedelta(days=retention - 1)
    if start_day >= oldest_safe:
        return RangePlan("redis", start_day, end_day)
    return RangePlan("hourly", start_day, end_day)


def image_path_to_url(image_path: str) -> Optional[str]:
    """Map a generator output path to its public URL, or None if unservable."""
    if not image_path or not image_path.startswith(_IMG_FS_PREFIX):
        return None
    return IMG_BASE + image_path[len(_IMG_FS_PREFIX):]


# --------------------------------------------------------------------------- #
# Member resolution (DB association — no WOM API round-trip)
# --------------------------------------------------------------------------- #

def resolve_group_member_ids(db_session, group_id: int) -> List[int]:
    """Member player_ids for a board: the group-association rows minus the
    group's ignored players (mirrors the web lootboard read path). The global
    group (2) means every tracked player."""
    from sqlalchemy import text as _text

    if group_id == 2:
        rows = db_session.execute(_text("SELECT player_id FROM players")).fetchall()
        return [int(r[0]) for r in rows]
    rows = db_session.execute(
        _text(
            "SELECT uga.player_id FROM user_group_association uga "
            "WHERE uga.group_id = :gid AND uga.player_id IS NOT NULL "
            "  AND uga.player_id NOT IN ("
            "    SELECT ip.player_id FROM ignored_players ip WHERE ip.group_id = :gid"
            "  )"
        ),
        {"gid": group_id},
    ).fetchall()
    return [int(r[0]) for r in rows]


# --------------------------------------------------------------------------- #
# Tier 1: Redis daily hashes
# --------------------------------------------------------------------------- #

_PIPELINE_CHUNK = 3000  # commands per pipeline round-trip


def fetch_timeframe_from_redis(
    redis_conn, player_ids: List[int], days: List[str], recent_cap: int = 50,
) -> Tuple[Dict[str, str], Dict[int, int], List[dict], int]:
    """Merge members' daily hashes into the renderer's 4-tuple:
    (group_items, player_totals, recent_drops, total_loot)."""
    import json as _json

    triplets = [(pid, day) for pid in player_ids for day in days]
    item_hashes: List[dict] = []
    player_totals: Dict[int, int] = {}
    recent_raw: List[Tuple[int, bytes]] = []  # (player_id, raw json)

    for offset in range(0, len(triplets), _PIPELINE_CHUNK // 3):
        chunk = triplets[offset: offset + _PIPELINE_CHUNK // 3]
        pipe = redis_conn.pipeline(transaction=False)
        for pid, day in chunk:
            pipe.hgetall(f"player:{pid}:daily:{day}:total_items")
            pipe.get(f"player:{pid}:daily:{day}:total_loot")
            pipe.lrange(f"player:{pid}:daily:{day}:recent_items", 0, -1)
        results = pipe.execute()
        for i, (pid, _day) in enumerate(chunk):
            item_hash, loot_raw, recents = results[i * 3], results[i * 3 + 1], results[i * 3 + 2]
            if item_hash:
                item_hashes.append(item_hash)
            if loot_raw:
                try:
                    player_totals[pid] = player_totals.get(pid, 0) + int(float(loot_raw))
                except (TypeError, ValueError):
                    pass
            for raw in (recents or []):
                recent_raw.append((pid, raw))

    merged = merge_item_hashes(item_hashes)
    group_items = format_group_items(merged)
    total_loot = sum(player_totals.values())

    # Recent drops: parse, attribute to the list's owner, dedupe, newest first.
    seen_drop_ids: set = set()
    recent_drops: List[dict] = []
    for pid, raw in recent_raw:
        try:
            data = _json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (ValueError, AttributeError, UnicodeDecodeError):
            continue
        drop_id = data.get("drop_id")
        if drop_id in seen_drop_ids:
            continue
        seen_drop_ids.add(drop_id)
        data.setdefault("player_id", pid)
        recent_drops.append(data)
    recent_drops.sort(key=lambda d: str(d.get("date_added", "")), reverse=True)
    return group_items, player_totals, recent_drops[:recent_cap], total_loot


# --------------------------------------------------------------------------- #
# Tier 2: hourly rollup table
# --------------------------------------------------------------------------- #

def missing_rollup_partitions(db_session, partitions: List[int]) -> List[int]:
    """Months in ``partitions`` with no rollup rows (backfill not there yet).

    The current month is always considered covered: the live tailer populates
    it from its first drop, and an empty result for it just means no loot yet.
    """
    from sqlalchemy import text as _text
    from sqlalchemy import bindparam as _bindparam

    if not partitions:
        return []
    rows = db_session.execute(
        _text(
            "SELECT DISTINCT `partition` FROM player_item_hourly_totals "
            "WHERE `partition` IN :parts"
        ).bindparams(_bindparam("parts", expanding=True)),
        {"parts": [int(p) for p in partitions]},
    ).fetchall()
    present = {int(r[0]) for r in rows}
    now = datetime.now()
    current = now.year * 100 + now.month
    return [p for p in partitions if p not in present and p != current]


def fetch_timeframe_from_hourly(
    db_session, player_ids: List[int], start_day: date, end_day: date,
) -> Tuple[Dict[str, str], Dict[int, int], List[dict], int]:
    """Aggregate the hourly rollup into the renderer's 4-tuple.

    Hour-string boundaries are inclusive of every hour of ``end_day`` (the
    table's ``date_hour`` is zero-padded ``%Y-%m-%d-%H``, so lexicographic
    BETWEEN is chronological). No recent-drops feed exists at this tier.
    """
    from sqlalchemy import text as _text
    from sqlalchemy import bindparam as _bindparam

    if not player_ids:
        return {}, {}, [], 0
    lo = datetime.combine(start_day, dtime.min).strftime("%Y-%m-%d-%H")
    hi = datetime.combine(end_day, dtime(23)).strftime("%Y-%m-%d-%H")

    item_rows = db_session.execute(
        _text(
            "SELECT item_id, SUM(quantity), SUM(total_value) "
            "FROM player_item_hourly_totals "
            "WHERE player_id IN :pids AND date_hour BETWEEN :lo AND :hi "
            "GROUP BY item_id"
        ).bindparams(_bindparam("pids", expanding=True)),
        {"pids": player_ids, "lo": lo, "hi": hi},
    ).fetchall()
    player_rows = db_session.execute(
        _text(
            "SELECT player_id, SUM(total_value) "
            "FROM player_item_hourly_totals "
            "WHERE player_id IN :pids AND date_hour BETWEEN :lo AND :hi "
            "GROUP BY player_id"
        ).bindparams(_bindparam("pids", expanding=True)),
        {"pids": player_ids, "lo": lo, "hi": hi},
    ).fetchall()

    group_items = {
        str(int(item_id)): f"{int(qty or 0)},{int(val or 0)}"
        for item_id, qty, val in item_rows
        if item_id is not None and (qty or val)
    }
    player_totals = {
        int(pid): int(val or 0) for pid, val in player_rows if val
    }
    total_loot = sum(player_totals.values())
    return group_items, player_totals, [], total_loot
