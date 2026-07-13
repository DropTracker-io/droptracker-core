"""
Backfill/rebuild `player_item_hourly_totals` for one or more YYYYMM partitions.

The rollup had no in-repo writer (same failure mode the NPC rollup had);
services/item_totals.py now tails new drops, and this script rebuilds history.

Run BEFORE (re)starting droptracker-player-updates so the tailer pointer is
seeded by the rebuild rather than racing it:

    python -m scripts.backfill_item_hourly_totals 202607
    python -m scripts.backfill_item_hourly_totals 202605 202606 202607
    python -m scripts.backfill_item_hourly_totals --from 202509   # through current month
"""

import argparse
import sys
import time

sys.path.insert(0, ".")

from services.item_totals import current_partition, rebuild_partition


def month_range(start: int, end: int):
    y, m = divmod(start, 100)
    while y * 100 + m <= end:
        yield y * 100 + m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("partitions", nargs="*", type=int, help="YYYYMM partitions to rebuild")
    parser.add_argument("--from", dest="from_partition", type=int,
                        help="rebuild every month from this YYYYMM through the current month")
    args = parser.parse_args()

    partitions = list(args.partitions)
    if args.from_partition:
        partitions.extend(month_range(args.from_partition, current_partition()))
    if not partitions:
        partitions = [current_partition()]
    partitions = sorted(set(partitions))

    for partition in partitions:
        started = time.time()
        rows = rebuild_partition(partition)
        print(f"partition {partition}: {rows} rollup rows in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
