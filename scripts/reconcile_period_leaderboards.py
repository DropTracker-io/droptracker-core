"""Reconcile the daily + weekly loot leaderboards for one ISO week from the database.

Why this exists
---------------
The weekly (``leaderboard:{YYYYWww}``) and daily (``leaderboard:{YYYYMMDD}``)
boards — global and per-group — are maintained incrementally (``ZINCRBY``) by
the drop intake path (``services/redis_updates.py``). Like ``leaderboard:all``
before it (see ``reconcile_all_time_leaderboards.py``), that path:

  * started from zero when the code shipped 2026-07-01 (no historical
    backfill), and
  * missed drops processed by intake workers that were still running the old
    code until the 2026-07-03 restart.

So the current week's boards under-count. This script rebuilds every daily
board of one ISO week plus the week's aggregate board from the authoritative
source: the ``drops`` table.

Definition of a player's score (must match ``force_update_player`` /
``_rebuild_period_leaderboards``):

    SUM(value * quantity) over every non-hidden drop in the day/week.

Per-group boards (``leaderboard:{token}:group:{gid}``) are rebuilt from the
same per-player sums restricted to each group's *current* membership — the
same rule the intake path applies. Group *totals* on the groups tab are
derived from the global player boards at read time, so they need no separate
write. Seasonal boards are out of scope (separate tables/namespace).

Usage
-----
    # dry run (default) — reports what would change, writes nothing
    python scripts/reconcile_period_leaderboards.py

    # actually write to Redis
    python scripts/reconcile_period_leaderboards.py --commit

    # a specific ISO week instead of the current one
    python scripts/reconcile_period_leaderboards.py --week 2026W26 --commit

Idempotent: re-running converges the boards to DB truth. Safe on a live
system; the only race is a drop landing between aggregation and the ZADD,
whose increment is overwritten — bounded by the run time (seconds) and
reconciled by any re-run or ``force_update_player``.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from services.redis_updates import RedisLootTracker  # noqa: E402
from utils.partitions import week_token  # noqa: E402
from utils.redis import redis_client  # noqa: E402

BATCH = 500
PK_CHUNK = 2_000_000  # rows per clustered-index range scan (see all-time script)

# Same retention the intake path applies (services/redis_updates.py).
DAILY_TTL = RedisLootTracker._DAILY_TTL
WEEKLY_TTL = RedisLootTracker._WEEKLY_TTL

# Dedicated engine: the shared app engine caps read_timeout at 30s, too short
# for range aggregations over the ~160M-row drops table.
_DB_USER = os.getenv("DB_USER")
_DB_PASS = os.getenv("DB_PASS")
_maint_engine = create_engine(
    f"mysql+pymysql://{_DB_USER}:{_DB_PASS}@localhost:3306/data",
    pool_size=1,
    max_overflow=1,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10, "read_timeout": 1800, "write_timeout": 60,
                  "charset": "utf8mb4", "autocommit": True},
)
MaintSession = sessionmaker(bind=_maint_engine)


def _fmt(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(n)


def _int_or_none(raw):
    try:
        return int(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


def _week_bounds(token: str) -> tuple[date, date]:
    """Monday (inclusive) and next Monday (exclusive) of an ISO week token."""
    m = re.fullmatch(r"(\d{4})W(\d{2})", token)
    if not m:
        raise SystemExit(f"invalid week token {token!r} (expected YYYYWww)")
    monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    return monday, monday + timedelta(days=7)


def _db_daily_totals(session, start: date, end: date) -> dict[str, dict[int, int]]:
    """``{YYYYMMDD: {player_id: SUM(value*quantity)}}`` over non-hidden drops.

    ``ix_drops_date_added`` makes the drop_id boundary lookups cheap; the
    aggregation itself walks the clustered index in bounded drop_id ranges so
    each query is a fast sequential scan.
    """
    lo, hi = session.execute(
        text("SELECT MIN(drop_id), MAX(drop_id) FROM drops "
             "WHERE date_added >= :a AND date_added < :b"),
        {"a": start, "b": end},
    ).one()
    if lo is None:
        return {}

    n_chunks = (hi - lo) // PK_CHUNK + 1
    print(f"  aggregating drop_id {lo:,}..{hi:,} in {n_chunks} chunk(s) of {PK_CHUNK:,}...")

    per_day: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    cursor = lo
    idx = 0
    while cursor <= hi:
        idx += 1
        chunk_end = cursor + PK_CHUNK
        rows = session.execute(
            text(
                "SELECT DATE_FORMAT(date_added, '%Y%m%d'), player_id, SUM(value * quantity) "
                "FROM drops "
                "WHERE drop_id >= :lo AND drop_id < :hi "
                "  AND date_added >= :a AND date_added < :b "
                "  AND hidden != 1 "
                "GROUP BY 1, 2"
            ),
            {"lo": cursor, "hi": chunk_end, "a": start, "b": end},
        ).fetchall()
        for day_key, pid, total in rows:
            if pid is not None and total is not None:
                per_day[day_key][pid] += int(total)
        print(f"  [{idx}/{n_chunks}] drop_id < {chunk_end:,}", flush=True)
        cursor = chunk_end

    # Keep positive sums only (mirrors the all-time reconcile).
    return {
        day: {pid: t for pid, t in totals.items() if t > 0}
        for day, totals in per_day.items()
    }


def _group_membership(session) -> dict[int, list[int]]:
    """player_id -> [group_id, ...] from current association rows."""
    rows = session.execute(
        text("SELECT player_id, group_id FROM user_group_association "
             "WHERE player_id IS NOT NULL")
    ).fetchall()
    members: dict[int, list[int]] = defaultdict(list)
    for pid, gid in rows:
        members[pid].append(gid)
    return members


def _reconcile_board(conn, key: str, totals: dict[int, int], ttl: int, commit: bool) -> tuple[int, int]:
    """Converge one sorted set to ``totals``. Returns (written, removed)."""
    existing = {
        pid: int(s)
        for m, s in conn.zrange(key, 0, -1, withscores=True)
        if (pid := _int_or_none(m)) is not None
    }
    stale = [pid for pid in existing if pid not in totals]
    changed = {pid: t for pid, t in totals.items() if existing.get(pid) != t}

    if commit and (changed or stale):
        items = list(changed.items())
        for i in range(0, len(items), BATCH):
            chunk = dict(items[i:i + BATCH])
            pipe = conn.pipeline(transaction=False)
            pipe.zadd(key, chunk)
            pipe.execute()
        if stale:
            for i in range(0, len(stale), BATCH):
                conn.zrem(key, *stale[i:i + BATCH])
        conn.expire(key, ttl)
    return len(changed), len(stale)


def reconcile(week: str, commit: bool) -> None:
    conn = redis_client.client
    if conn is None:
        print("ERROR: no Redis connection", file=sys.stderr)
        sys.exit(1)

    start, end = _week_bounds(week)
    today = date.today()
    print(f"Reconciling ISO week {week} ({start} .. {end - timedelta(days=1)}) "
          f"{'[COMMIT]' if commit else '[dry run]'}")

    session = MaintSession()
    try:
        per_day = _db_daily_totals(session, start, end)
        membership = _group_membership(session)
        # Per-group manual-submission exclusions (drop_group_moderation):
        # (token, group_id, player_id) -> GP to subtract from that GROUP board
        # only — global boards keep the full totals. Without this, every
        # reconcile run would leak policy-excluded manual drops back onto the
        # groups' boards.
        try:
            from services.drop_moderation import group_player_exclusion_totals_by_token
            deductions = group_player_exclusion_totals_by_token(
                session, {"day": set(per_day.keys()), "week": {week}}
            )
        except Exception as e:
            print(f"WARNING: couldn't load manual-policy exclusions: {e}", file=sys.stderr)
            deductions = {}
    finally:
        session.close()

    weekly: dict[int, int] = defaultdict(int)
    for totals in per_day.values():
        for pid, t in totals.items():
            weekly[pid] += t

    def _boards(token: str, totals: dict[int, int], ttl: int):
        """Yield (key, per-board totals, ttl) for the global + per-group boards."""
        yield f"leaderboard:{token}", totals, ttl
        by_group: dict[int, dict[int, int]] = defaultdict(dict)
        for pid, t in totals.items():
            for gid in membership.get(pid, ()):
                adjusted = t - deductions.get((token, gid, pid), 0)
                if adjusted > 0:
                    by_group[gid][pid] = adjusted
        for gid, g_totals in sorted(by_group.items()):
            yield f"leaderboard:{token}:group:{gid}", g_totals, ttl

    work: list[tuple[str, dict[int, int], int]] = []
    for day_key in sorted(per_day):
        work.extend(_boards(day_key, per_day[day_key], DAILY_TTL))
    work.extend(_boards(week, dict(weekly), WEEKLY_TTL))

    total_written = total_removed = 0
    for key, totals, ttl in work:
        written, removed = _reconcile_board(conn, key, totals, ttl, commit)
        total_written += written
        total_removed += removed
        if written or removed:
            board_sum = sum(totals.values())
            print(f"  {key}: {len(totals):,} members, sum={_fmt(board_sum)} "
                  f"(updated {written:,}, removed {removed:,})")

    day_sums = {d: sum(t.values()) for d, t in sorted(per_day.items())}
    print("\nPer-day DB truth:")
    for d, s in day_sums.items():
        marker = " (today — still accumulating)" if d == today.strftime("%Y%m%d") else ""
        print(f"  {d}: {_fmt(s)}{marker}")
    print(f"  {week}: {_fmt(sum(weekly.values()))} across {len(weekly):,} players")
    print(f"\n{'Wrote' if commit else 'Would write'} {total_written:,} member scores, "
          f"{'removed' if commit else 'would remove'} {total_removed:,} stale members "
          f"across {len(work):,} boards.")
    if not commit:
        print("DRY RUN — nothing written. Re-run with --commit to apply.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="write to Redis (default: dry run)")
    ap.add_argument("--week", default=week_token(),
                    help="ISO week token to reconcile (default: current, e.g. 2026W27)")
    args = ap.parse_args()
    reconcile(args.week, commit=args.commit)


if __name__ == "__main__":
    main()
