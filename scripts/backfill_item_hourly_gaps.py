"""Fill coverage gaps in `player_item_hourly_totals`, tailer-safe.

Why not scripts/backfill_item_hourly_totals.py? That rebuild runs one
month-sized INSERT..SELECT in a single transaction — correct in its intended
deploy flow (tailer stopped), but against the LIVE item-totals tailer the two
contend on the rollup's locks and the big statement dies with 1205 (lock wait
timeout), observed on the 202509 attempt. This filler chunks the same
aggregation BY DAY and commits per day: each statement holds locks for ~a
second, so it coexists with the tailer, and a failure loses at most one day.

Semantics match services/item_totals exactly (same GROUP BY, same value
definition, hidden drops included). An hour never spans two days, so every
(player,item,hour) group is fully computed within its day chunk — the upsert
writes ABSOLUTE values, making re-runs idempotent.

Usage:
    python -m scripts.backfill_item_hourly_gaps --months 202509 202510
    python -m scripts.backfill_item_hourly_gaps --from 202508 --to 202606
    ... add --commit to actually write (default is a dry-run row-count report).

Partial months (e.g. an interrupted earlier rebuild) are made consistent by
the same absolute upsert; rows that exist nowhere in `drops` anymore are NOT
deleted (this is a gap FILLER — use the owner rebuild script for that).
Never targets the current month: the live tailer owns it.
"""
from __future__ import annotations

import argparse
import calendar
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

_DAY_UPSERT_SQL = text(
    "INSERT INTO player_item_hourly_totals "
    "  (player_id, item_id, date_hour, `partition`, quantity, total_value, drop_count, last_drop_time) "
    "SELECT d.player_id, d.item_id, "
    "       DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), "
    "       :partition, "
    "       SUM(d.quantity), SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
    "FROM drops d "
    "WHERE d.date_added >= :day_start AND d.date_added < :day_end "
    "  AND d.item_id IS NOT NULL AND d.date_added IS NOT NULL "
    "GROUP BY d.player_id, d.item_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H') "
    "ON DUPLICATE KEY UPDATE "
    "  quantity = VALUES(quantity), "
    "  total_value = VALUES(total_value), "
    "  drop_count = VALUES(drop_count), "
    "  last_drop_time = VALUES(last_drop_time)"
)

_DAY_COUNT_SQL = text(
    "SELECT COUNT(DISTINCT d.player_id, d.item_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H')) "
    "FROM drops d "
    "WHERE d.date_added >= :day_start AND d.date_added < :day_end "
    "  AND d.item_id IS NOT NULL"
)

_RETRYABLE = ("1205", "Lock wait timeout", "Deadlock")


def month_days(partition: int):
    year, month = divmod(partition, 100)
    n_days = calendar.monthrange(year, month)[1]
    for day_num in range(1, n_days + 1):
        start = date(year, month, day_num)
        yield start, start + timedelta(days=1)


def month_range(start: int, end: int):
    y, m = divmod(start, 100)
    while y * 100 + m <= end:
        yield y * 100 + m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", nargs="*", type=int, default=[], help="YYYYMM partitions")
    ap.add_argument("--from", dest="from_p", type=int, help="start YYYYMM (inclusive)")
    ap.add_argument("--to", dest="to_p", type=int, help="end YYYYMM (inclusive)")
    ap.add_argument("--commit", action="store_true", help="write (default: dry-run counts)")
    ap.add_argument("--pace", type=float, default=1.0, help="sleep seconds between days")
    args = ap.parse_args()

    months = list(args.months)
    if args.from_p and args.to_p:
        months.extend(month_range(args.from_p, args.to_p))
    months = sorted(set(months))
    now = date.today()
    current = now.year * 100 + now.month
    months = [m for m in months if m < current]
    if not months:
        print("Nothing to do (no historical months selected).")
        return

    engine = create_engine(
        f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@localhost:3306/data",
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "read_timeout": 300, "write_timeout": 300,
                      "charset": "utf8mb4"},
    )
    Session = sessionmaker(bind=engine)

    grand_total = 0
    for partition in months:
        month_started = time.time()
        month_rows = 0
        for day_start, day_end in month_days(partition):
            params = {"partition": partition, "day_start": day_start, "day_end": day_end}
            for attempt in range(1, 4):
                s = Session()
                try:
                    if args.commit:
                        result = s.execute(_DAY_UPSERT_SQL, params)
                        s.commit()
                        month_rows += result.rowcount or 0
                    else:
                        month_rows += int(s.execute(_DAY_COUNT_SQL, params).scalar() or 0)
                    break
                except Exception as e:
                    s.rollback()
                    if attempt < 3 and any(tok in str(e) for tok in _RETRYABLE):
                        print(f"  {day_start}: lock contention, retry {attempt} in 10s")
                        time.sleep(10)
                        continue
                    raise
                finally:
                    s.close()
            time.sleep(args.pace)
        mode = "upserted" if args.commit else "would cover"
        print(f"partition {partition}: {mode} ~{month_rows} rollup rows "
              f"in {time.time() - month_started:.0f}s", flush=True)
        grand_total += month_rows

    print(f"{'DONE' if args.commit else 'DRY RUN'}: ~{grand_total} rollup rows across {len(months)} month(s)")


if __name__ == "__main__":
    main()
