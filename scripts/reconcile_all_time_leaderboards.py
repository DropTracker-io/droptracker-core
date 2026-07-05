"""Reconcile the all-time global loot leaderboard from the database.

Why this exists
---------------
The monthly leaderboard (``leaderboard:{YYYYMM}``) is kept accurate by the repair
paths in ``services/redis_updates.py`` (``force_update_player`` /
``sync_leaderboards_from_redis``). The all-time board (``leaderboard:all``) was
added later as an *incremental-only* ``ZINCRBY`` in the drop intake path, so it:

  * started from zero when that code shipped (no historical backfill), and
  * is never corrected by any repair path.

The result is that ``leaderboard:all`` under-counts badly — for some players it
is even smaller than a single month. This script rebuilds it (and the per-player
``player:{id}:all:total_loot`` strings) from the authoritative source: the
``drops`` table.

Definition of all-time loot (must match ``force_update_player``):

    SUM(value * quantity) over every non-hidden drop for the player.

Group all-time totals are intentionally NOT precomputed here: the Web API derives
them from these per-player all-time scores at read time
(``_compute_group_totals``), so fixing the player board fixes the group board too.

Usage
-----
    # dry run (default) — reports what would change, writes nothing
    python scripts/reconcile_all_time_leaderboards.py

    # actually write to Redis
    python scripts/reconcile_all_time_leaderboards.py --commit

    # inspect a single player without touching Redis
    python scripts/reconcile_all_time_leaderboards.py --player iiButler

Idempotent: re-running converges ``leaderboard:all`` to DB truth. Safe to run on
a live system; the only race is a drop landing mid-run, which the next
``force_update_player`` (or a re-run) reconciles.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db import Player, Drop  # noqa: E402
from utils.redis import redis_client  # noqa: E402

ALL_KEY = "leaderboard:all"
BATCH = 500

# The shared app engine caps read_timeout at 30s (db/models/base.py), which is far
# too short for the per-partition GROUP BYs this backfill runs over a ~160M-row
# table. Use a dedicated engine with a generous timeout so the heavy aggregation
# queries can complete without competing with the app's connection pool.
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


# Rows per primary-key range chunk. 2M rows ≈ 4-5s per query (measured); the
# whole ~164M-row table completes in a few minutes.
PK_CHUNK = 2_000_000


def _db_all_time_totals(session) -> dict[int, int]:
    """player_id -> SUM(value*quantity) over non-hidden drops (>0 only).

    The ``drops`` table has ~160M rows, so a single ``GROUP BY player_id`` over
    the whole table times out — and so do per-``partition`` GROUP BYs for recent
    (large) months, because ``ix_drops_partition`` forces a random row lookup per
    matching drop. Instead we walk the table in primary-key (``drop_id``) ranges:
    each chunk is a sequential clustered-index scan (bounded, fast regardless of
    table position), and the per-player sums are merged in Python.
    """
    lo, hi = session.execute(text("SELECT MIN(drop_id), MAX(drop_id) FROM drops")).one()
    if lo is None:
        return {}
    n_chunks = (hi - lo) // PK_CHUNK + 1
    print(f"  aggregating drop_id {lo:,}..{hi:,} in {n_chunks} chunks of {PK_CHUNK:,}...")

    totals: dict[int, int] = {}
    start = lo
    idx = 0
    while start <= hi:
        idx += 1
        end = start + PK_CHUNK
        rows = session.execute(
            text(
                "SELECT player_id, SUM(value * quantity) FROM drops "
                "WHERE drop_id >= :a AND drop_id < :b AND hidden != 1 "
                "GROUP BY player_id"
            ),
            {"a": start, "b": end},
        ).fetchall()
        for pid, total in rows:
            if pid is not None and total is not None:
                totals[pid] = totals.get(pid, 0) + int(total)
        print(f"  [{idx}/{n_chunks}] drop_id < {end:,}: "
              f"{len(totals):,} players accumulated", flush=True)
        start = end

    # Drop any players whose summed all-time loot is non-positive.
    return {pid: t for pid, t in totals.items() if t > 0}


def inspect_player(session, conn, name: str) -> None:
    p = session.query(Player.player_id, Player.player_name).filter(
        Player.player_name.ilike(name)
    ).first()
    if not p:
        print(f"No player found matching {name!r}")
        return
    pid, pname = p
    db_total = int(
        session.query(func.coalesce(func.sum(Drop.value * Drop.quantity), 0))
        .filter(Drop.player_id == pid, Drop.hidden != True)  # noqa: E712
        .scalar()
    )
    redis_score = conn.zscore(ALL_KEY, pid)
    redis_score = int(redis_score) if redis_score is not None else None
    print(f"{pname} (id={pid}):")
    print(f"  DB all-time      = {db_total:,} ({_fmt(db_total)})")
    print(
        "  leaderboard:all  = "
        + (f"{redis_score:,} ({_fmt(redis_score)})" if redis_score is not None else "<absent>")
    )


def reconcile(commit: bool) -> None:
    conn = redis_client.client
    if conn is None:
        print("ERROR: no Redis connection", file=sys.stderr)
        sys.exit(1)

    session = MaintSession()
    try:
        print("Reading all-time totals from the drops table...")
        totals = _db_all_time_totals(session)
        print(f"  {len(totals):,} players with positive all-time loot")

        existing = {
            int(m): int(s)
            for m, s in conn.zrange(ALL_KEY, 0, -1, withscores=True)
            if _int_or_none(m) is not None
        }
        stale = [pid for pid in existing if pid not in totals]

        before_sum = sum(existing.values())
        after_sum = sum(totals.values())
        print("\nSummary:")
        print(f"  members  before={len(existing):,}  after={len(totals):,}")
        print(f"  sum      before={before_sum:,} ({_fmt(before_sum)})  "
              f"after={after_sum:,} ({_fmt(after_sum)})")
        print(f"  stale members to remove (no visible drops): {len(stale):,}")

        if not commit:
            print("\nDRY RUN — nothing written. Re-run with --commit to apply.")
            return

        print("\nWriting leaderboard:all + per-player all-time totals...")
        items = list(totals.items())
        for i in range(0, len(items), BATCH):
            chunk = items[i:i + BATCH]
            pipe = conn.pipeline(transaction=False)
            pipe.zadd(ALL_KEY, {pid: total for pid, total in chunk})
            for pid, total in chunk:
                pipe.set(f"player:{pid}:all:total_loot", total)
            pipe.execute()

        if stale:
            for i in range(0, len(stale), BATCH):
                chunk = stale[i:i + BATCH]
                pipe = conn.pipeline(transaction=False)
                pipe.zrem(ALL_KEY, *chunk)
                for pid in chunk:
                    pipe.delete(f"player:{pid}:all:total_loot")
                pipe.execute()

        print(f"Done. leaderboard:all now has {conn.zcard(ALL_KEY):,} members.")
    finally:
        session.close()


def _int_or_none(raw):
    try:
        return int(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="write to Redis (default: dry run)")
    ap.add_argument("--player", metavar="NAME", help="inspect a single player and exit")
    args = ap.parse_args()

    if args.player:
        session = MaintSession()
        try:
            inspect_player(session, redis_client.client, args.player)
        finally:
            session.close()
        return

    reconcile(commit=args.commit)


if __name__ == "__main__":
    main()
