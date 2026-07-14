"""One-off Redis hygiene sweep (2026-07 audit).

Fixes the fallout of two hot-path bugs (both since fixed in code):

1. ``player:{id}:daily:{YYYYMMDD}:total_items|total_loot|recent_items`` were
   created by ``_add_drop_incremental`` with NO TTL (only the rare
   ``force_update_player`` rebuild path set one). ~98k permanent daily keys
   (~200MB) accumulated between 2025-11 and 2026-07. Policy applied here,
   matching ``_rebuild_daily_data``'s 90-day retention:
     - key date older than 90 days  -> DELETE now
     - key date within 90 days      -> EXPIRE at (date + 90d)

2. The drop processor's item-exists cache used the bare item id as both key
   and value (``SET 4151 4151``, no TTL) — ~4k permanent bare-number keys.
   The cache now lives at ``item:known:{id}`` (7d TTL); this deletes the old
   bare keys (only when value == key, so nothing else can be caught).

Usage:
    venv/bin/python -m scripts.redis_ttl_cleanup           # dry run (default)
    venv/bin/python -m scripts.redis_ttl_cleanup --apply   # actually mutate
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()
import redis  # noqa: E402

DAILY_TTL_DAYS = 90
DAILY_RE = re.compile(r"^player:\d+:daily:(\d{8}):(total_items|total_loot|recent_items)$")
BARE_RE = re.compile(r"^\d+$")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="mutate Redis (default is dry run)")
    args = parser.parse_args()

    r = redis.Redis(host="localhost", port=6379, db=0, password=os.getenv("DB_PASS"))
    now = datetime.now()
    cutoff = now - timedelta(days=DAILY_TTL_DAYS)

    deleted = expired = bare_deleted = scanned = 0

    # --- 1. daily player keys without TTL ---------------------------------
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match="player:*:daily:*", count=5000)
        if keys:
            pipe = r.pipeline(transaction=False)
            for k in keys:
                pipe.ttl(k)
            ttls = pipe.execute()

            actions = r.pipeline(transaction=False)
            for k, ttl in zip(keys, ttls):
                if ttl >= 0:
                    continue  # already volatile
                m = DAILY_RE.match(k.decode())
                if not m:
                    continue
                scanned += 1
                try:
                    key_date = datetime.strptime(m.group(1), "%Y%m%d")
                except ValueError:
                    continue
                if key_date < cutoff:
                    deleted += 1
                    if args.apply:
                        actions.delete(k)
                else:
                    remaining = int((key_date + timedelta(days=DAILY_TTL_DAYS) - now).total_seconds())
                    expired += 1
                    if args.apply:
                        actions.expire(k, max(remaining, 3600))
            if args.apply:
                actions.execute()
        if cursor == 0:
            break

    # --- 2. legacy bare-number item cache keys -----------------------------
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match="[0-9]*", count=5000)
        candidates = [k for k in keys if BARE_RE.match(k.decode())]
        if candidates:
            pipe = r.pipeline(transaction=False)
            for k in candidates:
                pipe.get(k)
            values = pipe.execute()
            actions = r.pipeline(transaction=False)
            for k, v in zip(candidates, values):
                if v is not None and v == k:  # SET <id> <id> signature only
                    bare_deleted += 1
                    if args.apply:
                        actions.delete(k)
            if args.apply:
                actions.execute()
        if cursor == 0:
            break

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"[{mode}] no-TTL daily keys found: {scanned}")
    print(f"[{mode}]   -> deleted (older than {DAILY_TTL_DAYS}d): {deleted}")
    print(f"[{mode}]   -> EXPIRE backfilled: {expired}")
    print(f"[{mode}] bare item-cache keys deleted: {bare_deleted}")
    if not args.apply:
        print("Re-run with --apply to make these changes.")


if __name__ == "__main__":
    sys.exit(main())
