"""
Seed the per-NPC "last received per item" Redis registry from the drops table.

The web NPC drop-table endpoint (web_api/routes/npcs.py) keeps a Redis hash
per NPC (``npc:{id}:last_item_drops``: item_id -> latest drop, plus a
``_cursor`` field holding the highest drop_id scanned) and tops it up
incrementally per request. Cold registries for busy NPCs would otherwise need
a multi-minute per-NPC scan (item_id isn't in ix_drops_npc_id), so this script
does ONE sequential pass over the whole drops table (~167M rows in PK-range
chunks) and writes every NPC's registry in a single run.

Safe to re-run (idempotent; overwrites with fresher data). Read-only on MySQL;
writes only the npc:*:last_item_drops Redis hashes.

    python -m scripts.backfill_npc_last_drops              # full run
    python -m scripts.backfill_npc_last_drops --chunk 500000
"""

import argparse
import sys
import time

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from web_api.routes.npcs import _store_last_drops


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", type=int, default=1_000_000,
                        help="drop_id range width per query (default 1M)")
    args = parser.parse_args()

    s = Session()
    try:
        max_id = s.execute(text("SELECT COALESCE(MAX(drop_id),0) FROM drops")).scalar() or 0
        if not max_id:
            print("drops table is empty; nothing to do")
            return

        print(f"scanning drops 1..{max_id:,} in chunks of {args.chunk:,}")
        # (npc_id, item_id) -> latest entry; ascending scan means last write wins.
        latest: dict[tuple, dict] = {}
        started = time.time()
        lo = 0
        while lo < max_id:
            hi = min(lo + args.chunk, max_id)
            rows = s.execute(
                text(
                    "SELECT drop_id, npc_id, item_id, player_id, value, quantity, date_added "
                    "FROM drops WHERE drop_id > :lo AND drop_id <= :hi "
                    "AND npc_id IS NOT NULL AND item_id IS NOT NULL AND player_id IS NOT NULL"
                ),
                {"lo": lo, "hi": hi},
            ).fetchall()
            for drop_id, npc_id, item_id, player_id, value, quantity, date_added in rows:
                try:
                    ts = int(date_added.timestamp()) if date_added else 0
                except Exception:
                    ts = 0
                latest[(int(npc_id), int(item_id))] = {
                    "drop_id": int(drop_id),
                    "player_id": int(player_id),
                    "ts": ts,
                    "value": int(value or 0),
                    "quantity": int(quantity or 1),
                }
            lo = hi
            elapsed = time.time() - started
            print(f"  ..{hi:,} / {max_id:,} ({hi / max_id:5.1%})  "
                  f"pairs={len(latest):,}  {elapsed:6.0f}s", flush=True)
    finally:
        s.close()

    # Regroup per NPC and write each registry hash with cursor = scan ceiling.
    by_npc: dict[int, dict] = {}
    for (npc_id, item_id), entry in latest.items():
        by_npc.setdefault(npc_id, {})[item_id] = entry

    print(f"writing {len(by_npc):,} NPC registries to Redis (cursor={max_id:,})")
    for npc_id, items in by_npc.items():
        _store_last_drops(npc_id, items, max_id)
    print("done")


if __name__ == "__main__":
    main()
