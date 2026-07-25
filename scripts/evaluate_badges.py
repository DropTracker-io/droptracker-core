"""Run the badge award engine manually (testing / ops entry point).

The hourly bot task (bots/main.py) calls the same ``run_badge_cycle``; this
script exists for dry-runs, re-processing a specific day, or running one
evaluator family in isolation. All evaluators are idempotent, so re-running
any day is safe.

Usage
-----
    # dry run for yesterday (writes nothing, logs intended awards)
    python scripts/evaluate_badges.py --dry-run

    # actually award, using the normal marker-based day logic
    python scripts/evaluate_badges.py

    # a specific day / one evaluator family
    python scripts/evaluate_badges.py --day 20260703 --only daily --dry-run
    python scripts/evaluate_badges.py --only records

    # the held global loot leader badges (all-time + monthly); --month pins
    # the monthly one to a specific board instead of the current month
    python scripts/evaluate_badges.py --only leaders --dry-run
    python scripts/evaluate_badges.py --only leaders --month 202606
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
    ap = argparse.ArgumentParser(description="Evaluate and award badges.")
    ap.add_argument("--dry-run", action="store_true",
                    help="log intended awards without writing anything")
    ap.add_argument("--day", metavar="YYYYMMDD",
                    help="process this specific day instead of the marker logic")
    ap.add_argument("--month", metavar="YYYYMM",
                    help="converge the monthly leader badge against this month "
                         "instead of the current one")
    ap.add_argument("--only", choices=["daily", "streaks", "records", "leaders"],
                    help="restrict to one evaluator family")
    args = ap.parse_args()

    days = None
    if args.day:
        datetime.strptime(args.day, "%Y%m%d")  # validate
        days = [args.day]
    elif args.dry_run:
        # Dry runs shouldn't depend on (or advance) the marker.
        days = [day_token(datetime.now() - timedelta(days=1))]

    months = None
    if args.month:
        datetime.strptime(args.month, "%Y%m")  # validate
        months = [args.month]

    stats = run_badge_cycle(dry_run=args.dry_run, days=days, only=args.only,
                            months=months)
    print(f"[evaluate_badges] done: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
