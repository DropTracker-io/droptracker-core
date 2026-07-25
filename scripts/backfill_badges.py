"""One-time backfill of badge awards from recent history.

Covers:
  * daily loot champions for the last N days (default 85 — safety margin under
    the 90-day TTL on the ``leaderboard:{YYYYMMDD}`` Redis sets),
  * streak badges evaluated against the most recent complete day (players who
    are mid-streak get picked up by the nightly cycle going forward),
  * boss records, which converge over the full ``personal_best`` history
    natively (no window needed),
  * the held global loot leaders — the all-time board, plus one monthly slot
    per month the backfill window touches (those boards are persistent, so a
    past month converges to its true final winner).

Days whose daily sets have expired or are empty are skipped silently.

Deliberately NOT implemented in v1: reconstructing daily champions older than
the Redis window from the ``drops`` table. It is computable (partition-pruned
per-day aggregates), but per-day scans over a ~160M-row table are expensive
and the product value is marginal. If ever needed, add an opt-in
``--from-drops --month YYYYMM`` mode modeled on
``scripts/reconcile_period_leaderboards.py``'s chunked scanning.

Usage
-----
    python scripts/backfill_badges.py            # dry run (default)
    python scripts/backfill_badges.py --commit   # actually write awards
    python scripts/backfill_badges.py --days 30 --commit
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.badges import run_badge_cycle  # noqa: E402
from utils.partitions import day_token  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill badge awards from recent history.")
    ap.add_argument("--commit", action="store_true",
                    help="actually write awards (default is a dry run)")
    ap.add_argument("--days", type=int, default=85,
                    help="how many days of daily champions to backfill (default 85)")
    args = ap.parse_args()
    dry_run = not args.commit

    now = datetime.now()
    days = [day_token(now - timedelta(days=offset)) for offset in range(args.days, 0, -1)]

    print(f"[backfill_badges] {'DRY-RUN ' if dry_run else ''}processing {len(days)} day(s): "
          f"{days[0]}..{days[-1]}")
    stats = run_badge_cycle(dry_run=dry_run, days=days)
    print(f"[backfill_badges] done: daily={stats['daily']} streaks={stats['streaks']} "
          f"records={stats['records']} leaders={stats['leaders']}")
    if dry_run:
        print("[backfill_badges] dry run — re-run with --commit to write awards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
