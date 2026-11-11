"""Nightly retention for player_stat_snapshots.

    venv/bin/python -m scripts.stat_snapshot_retention            # dry run
    venv/bin/python -m scripts.stat_snapshot_retention --apply

Adds upcoming month partitions, then for each day older than 90 days:
archives the day's rows to B2 (dt_stats/snapshots/YYYY/MM/DAY-<hash>.jsonl.gz;
the bucket is public through the CDN, so the name carries a keyed hash),
reads the archive back, and only then thins the day to one row per player
plus pinned rows. See utils/stat_snapshot_retention.py for the rules.

Exits non-zero if an archive could not be written or verified; nothing is
deleted for that day and the next run retries it.
"""
import argparse
import os
import sys
from datetime import date


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write; without it, report only")
    parser.add_argument("--max-days", type=int, default=14,
                        help="most days to process in one run (default 14)")
    args = parser.parse_args()

    from db.models import session
    from utils import image_storage
    from utils import stat_snapshot_retention as retention
    from utils.redis import redis_client

    # image_storage has loaded .env by now. The archive names are keyed with
    # the B2 application key so they cannot be guessed from the date.
    secret = os.getenv("B2_APPLICATION_KEY", "")
    if args.apply and not secret:
        print("B2_APPLICATION_KEY is not set: refusing to archive under a guessable name",
              file=sys.stderr)
        return 1

    def put(key, data):
        return image_storage.put_bytes(key, data, "application/gzip")

    try:
        report = retention.run(
            session, date.today(), apply=args.apply, max_days=args.max_days,
            put=put, get=image_storage.get_bytes,
            read_watermark=lambda: redis_client.client.get(retention.WATERMARK_KEY),
            write_watermark=lambda day: redis_client.client.set(retention.WATERMARK_KEY, day),
            secret=secret,
        )
    except retention.ArchiveError as exc:
        print(f"ARCHIVE FAILED, nothing deleted for that day: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    mode = "applied" if args.apply else "dry run"
    print(f"done ({mode}): {len(report['days'])} days looked at, "
          f"{sum(d['deleted'] for d in report['days'].values())} rows deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
