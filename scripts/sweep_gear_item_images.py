"""Backfill item icons referenced by stored gear loadouts.

``scripts/backfill_item_images.py`` walks the ``items`` table, which only ever
contains items somebody has *submitted* — a drop, a collection log slot, a pet.
Worn gear is different: a player can set a personal best in an item that has
never been submitted by anyone, so its id appears in
``personal_best_loadouts`` and nowhere else. That item's icon is therefore
never fetched by any of the existing automation, and the website renders the
slot as the placeholder GIF indefinitely.

This sweep closes that gap from the other direction: collect every item id the
stored loadouts actually reference, keep the ones with no icon on disk, and
fetch them from the RuneLite cache.

Going forward the ingest path (``data/submissions/pb._store_loadout`` ->
``utils.item_images.ensure_item_images``) fetches these as loadouts arrive, so
this is a safety net rather than the primary mechanism — it recovers ids that
were stored before that hook existed, or whose download failed transiently.

Usage:
    ./venv/bin/python3 -m scripts.sweep_gear_item_images [--apply] [--concurrency N]

Dry-run by default, like every maintenance script here. Idempotent.
"""

import argparse
import asyncio
import json
import os
import sys

import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.item_images import (  # noqa: E402
    ITEMDB_DIR,
    ensure_item_images,
    item_image_path,
)


def _referenced_item_ids() -> tuple[set[int], set[int]]:
    """Every item id named by a stored loadout, split (equipment, inventory).

    Reads only ``personal_best_loadouts`` — a few thousand short rows. It
    deliberately never touches ``drops``, which is the large hot table.
    """
    conn = pymysql.connect(
        host="localhost",
        port=3306,
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        database="data",
        charset="utf8mb4",
    )
    equipment: set[int] = set()
    inventory: set[int] = set()
    try:
        cur = conn.cursor()
        cur.execute("SELECT equipment, inventory FROM personal_best_loadouts")
        for raw_equipment, raw_inventory in cur.fetchall():
            for raw, sink in ((raw_equipment, equipment), (raw_inventory, inventory)):
                if not raw:
                    continue
                try:
                    entries = json.loads(raw)
                except (TypeError, ValueError):
                    # A corrupt row must not abort the sweep.
                    continue
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("item_id"), int):
                        sink.add(entry["item_id"])
    finally:
        conn.close()
    return equipment, inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="actually download (default: dry run)"
    )
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    equipment, inventory = _referenced_item_ids()
    referenced = equipment | inventory
    missing = sorted(
        i for i in referenced if i >= 0 and not os.path.exists(item_image_path(i))
    )

    print(f"referenced by loadouts: {len(referenced)} distinct item ids")
    print(f"  worn equipment: {len(equipment)}   inventory: {len(inventory)}")
    print(f"missing an icon on disk: {len(missing)}")
    if missing:
        preview = ", ".join(str(i) for i in missing[:20])
        print(f"  e.g. {preview}{' ...' if len(missing) > 20 else ''}")

    if not missing:
        return 0
    if not args.apply:
        print("\ndry run — re-run with --apply to download")
        return 0

    if not os.access(ITEMDB_DIR, os.W_OK):
        # The failure mode this whole sweep exists to fix was an unwritable
        # icon directory, so refuse to "succeed" quietly against one.
        print(f"\nERROR: {ITEMDB_DIR} is not writable by this account.")
        return 1

    fetched = asyncio.run(ensure_item_images(missing, concurrency=args.concurrency))
    still_missing = [i for i in missing if not os.path.exists(item_image_path(i))]
    print(f"\nfetched: {fetched}")
    print(f"still missing: {len(still_missing)}")
    if still_missing:
        # Expected for a handful of ids: RuneLite's cache has no icon for game
        # placeholders and some variant ids. Reported, not treated as failure.
        print("  " + ", ".join(str(i) for i in still_missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
