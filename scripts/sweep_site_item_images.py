"""Backfill item icons the website can display but nothing else fetches.

``scripts/backfill_item_images.py`` walks the ``items`` table, which only ever
contains items somebody has *submitted*. Two surfaces display item ids that
were never submitted by anyone, so neither is reached by that backfill nor by
the icon fetch on the item-lookup path:

* **Worn gear on a personal best.** A player can set a best time in an item
  nobody has ever dropped; its id lives in ``personal_best_loadouts`` and
  nowhere else.
* **Collection log slots.** A profile renders every slot the *game* defines,
  obtained or not — that is the point of a collection log — so it needs icons
  for the whole catalogue rather than for what has been submitted.

Both rendered as the placeholder for exactly the same reason, and this sweep
closes both from the same direction: collect every id the site can actually
display, keep the ones with no icon on disk, fetch them from the RuneLite
cache.

Gear is also covered as it arrives by the ingest hook
(``data/submissions/pb._store_loadout`` -> ``utils.item_images``), so for gear
this is a safety net. For the collection log it is the primary mechanism: the
catalogue changes only when the game does, which a daily pass tracks fine.

Usage:
    ./venv/bin/python3 -m scripts.sweep_site_item_images [--apply] [--concurrency N]

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


def _loadout_item_ids(cur) -> tuple[set[int], set[int]]:
    """Item ids named by a stored PB loadout, split (equipment, inventory).

    Reads only ``personal_best_loadouts`` — a few thousand short rows. It
    deliberately never touches ``drops``, which is the large hot table.
    """
    equipment: set[int] = set()
    inventory: set[int] = set()
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
    return equipment, inventory


def _collection_log_item_ids(cur) -> set[int]:
    """Every item id the collection log defines a slot for.

    The third id space that nothing else fetches icons for, and the one behind
    the clog slots that rendered blank on player profiles. A profile shows every
    slot the *game* defines, obtained or not — that is the point of a collection
    log — so the icons it needs are not the icons anybody has submitted. Most of
    these ids have no ``items`` row and appear in no loadout, which is exactly
    why neither the item backfill nor the loadout sweep reached them.

    Sourced from the plugin manifest that ``scripts/sync_collection_log.py``
    populates, which is the same structure the profile endpoint renders from —
    so this covers precisely what the page can display, no more and no less.
    """
    ids: set[int] = set()
    cur.execute(
        "SELECT payload FROM plugin_manifest_sections WHERE `key` = 'collection_log'"
    )
    row = cur.fetchone()
    if not row or not row[0]:
        # The structure sync has not run on this box; nothing to check.
        return ids
    try:
        tabs = json.loads(row[0])
    except (TypeError, ValueError):
        return ids
    if not isinstance(tabs, list):
        return ids
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        for page in tab.get("pages", []) or []:
            if not isinstance(page, dict):
                continue
            for item_id in page.get("items", []) or []:
                if isinstance(item_id, int):
                    ids.add(item_id)
    return ids


def _referenced_item_ids() -> tuple[set[int], set[int], set[int]]:
    """(equipment, inventory, collection log) item ids the site can display."""
    conn = pymysql.connect(
        host="localhost",
        port=3306,
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        database="data",
        charset="utf8mb4",
    )
    try:
        cur = conn.cursor()
        equipment, inventory = _loadout_item_ids(cur)
        clog = _collection_log_item_ids(cur)
    finally:
        conn.close()
    return equipment, inventory, clog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="actually download (default: dry run)"
    )
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    equipment, inventory, clog = _referenced_item_ids()
    referenced = equipment | inventory | clog
    missing = sorted(
        i for i in referenced if i >= 0 and not os.path.exists(item_image_path(i))
    )

    print(f"referenced by the site: {len(referenced)} distinct item ids")
    print(f"  worn equipment: {len(equipment)}   inventory: {len(inventory)}")
    print(f"  collection log slots: {len(clog)}")
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
