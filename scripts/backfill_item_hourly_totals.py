"""
Backfill/rebuild `player_item_hourly_totals` for one or more YYYYMM partitions.

The rollup had no in-repo writer (same failure mode the NPC rollup had);
services/item_totals.py now tails new drops, and this script rebuilds history.

Run BEFORE (re)starting droptracker-player-updates so the tailer pointer is
seeded by the rebuild rather than racing it:

    python -m scripts.backfill_item_hourly_totals 202607
    python -m scripts.backfill_item_hourly_totals 202605 202606 202607
    python -m scripts.backfill_item_hourly_totals --from 202509   # through current month

Day-chunked by default, and that matters
----------------------------------------
``rebuild_partition`` recomputes a month in ONE statement filtered on
``drops.partition``. There is no (player_id, partition) composite index, so
that scans the whole month across every player and **read-timeouts** on a
200M-row table. It fails *after* the DELETE, which leaves the partition
emptier than it started.

``--whole-partition`` restores the single-statement behaviour for a small
month where it is genuinely faster. This mirrors
``scripts/backfill_npc_hourly_totals.py``, which hit the same wall first.
"""

import argparse
import sys
import time

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from services.item_totals import (
    _month_days,
    current_partition,
    get_pointer,
    rebuild_partition,
    refold_day,
    set_pointer,
)


def month_range(start: int, end: int):
    y, m = divmod(start, 100)
    while y * 100 + m <= end:
        yield y * 100 + m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def backfill_chunked(partition: int, session, *, pace: float = 0.0) -> int:
    """Rebuild one partition day by day. Returns rows affected.

    Deliberately does NOT delete the partition first. ``refold_day`` upserts,
    so every bucket it touches is absolutely recomputed; a delete would only be
    needed to drop buckets that no longer have any drops, which cannot happen
    for a settled month. Skipping it means an interrupted run never leaves a
    hole — the failure mode that produced these gaps in the first place.
    """
    max_id = session.execute(
        text("SELECT COALESCE(MAX(drop_id), 0) FROM drops")
    ).scalar()
    affected = 0
    for day_start, day_end in _month_days(partition):
        started = time.time()
        rows = refold_day(session, partition, day_start, day_end, max_id)
        affected += rows
        print(f"  {day_start}: {rows} rows in {time.time() - started:.1f}s", flush=True)
        if pace:
            time.sleep(pace)

    # Match rebuild_partition: move the tailer pointer up so it doesn't re-fold
    # the window we just recomputed.
    pointer = get_pointer()
    if pointer is None or pointer < max_id:
        set_pointer(max_id)
    return affected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("partitions", nargs="*", type=int, help="YYYYMM partitions to rebuild")
    parser.add_argument("--from", dest="from_partition", type=int,
                        help="rebuild every month from this YYYYMM through the current month")
    parser.add_argument("--whole-partition", action="store_true",
                        help="one statement per month instead of per day (small months only)")
    parser.add_argument("--pace", type=float, default=0.0, metavar="SECONDS",
                        help="sleep between days, to keep load off a busy server")
    args = parser.parse_args()

    partitions = list(args.partitions)
    if args.from_partition:
        partitions.extend(month_range(args.from_partition, current_partition()))
    if not partitions:
        partitions = [current_partition()]
    partitions = sorted(set(partitions))

    session = Session()
    try:
        for partition in partitions:
            started = time.time()
            if args.whole_partition:
                rows = rebuild_partition(partition)
            else:
                rows = backfill_chunked(partition, session, pace=args.pace)
            print(f"partition {partition}: {rows} rollup rows in "
                  f"{time.time() - started:.1f}s", flush=True)
    finally:
        session.close()


if __name__ == "__main__":
    main()
