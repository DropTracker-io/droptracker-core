"""Backfill missing item icons.

Ensures every item in the `items` table has an on-disk icon at
``static/assets/img/itemdb/{id}.png``, in three idempotent stages:

  1. Download the RuneLite icon for the id directly.
  2. Placeholder/variant link: RuneLite has no icon for game "placeholder" and
     stacked/Leagues-variant ids (they render using a base item's icon). For a
     still-missing id, copy the icon of the nearest id that shares the same
     item_name and already has one.
  3. Wiki resolve: for the remainder, ask the OSRS Wiki for the item's
     canonical id, download that icon, and copy it into place.

Safe to re-run: existing icons are skipped, negative/sentinel ids are ignored,
and failures are reported without aborting.

Usage:
    ./venv/bin/python3 -m scripts.backfill_item_images [--dry-run] [--concurrency N] [--no-wiki]
"""

import argparse
import asyncio
import os
import shutil
import sys

import aiohttp
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.item_images import ITEMDB_DIR, RUNELITE_ICON_URL, item_image_path  # noqa: E402


def _all_items() -> dict[int, str]:
    conn = pymysql.connect(
        host="localhost",
        port=3306,
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        database="data",
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT item_id, item_name FROM items")
        return {i: n for i, n in cur.fetchall() if i is not None}
    finally:
        conn.close()


def _missing(byid: dict[int, str]) -> list[int]:
    return sorted(i for i in byid if i >= 0 and not os.path.exists(item_image_path(i)))


def _link_placeholders(byid: dict[int, str], window: int = 12) -> int:
    """Stage 2: copy the nearest same-name base icon onto placeholder/variant ids."""
    linked = 0
    for iid in _missing(byid):
        name = byid[iid]
        found = None
        for d in range(1, window + 1):
            for cand in (iid - d, iid + d):
                if byid.get(cand) == name and os.path.exists(item_image_path(cand)):
                    found = cand
                    break
            if found:
                break
        if found is not None:
            shutil.copyfile(item_image_path(found), item_image_path(iid))
            linked += 1
    return linked


async def _download_one(session: aiohttp.ClientSession, iid: int) -> tuple[int, str]:
    url = RUNELITE_ICON_URL.format(item_id=iid)
    path = item_image_path(iid)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return iid, f"http_{resp.status}"
            data = await resp.read()
        if not data:
            return iid, "empty"
        os.makedirs(ITEMDB_DIR, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return iid, "ok"
    except Exception as e:  # noqa: BLE001
        return iid, f"error:{type(e).__name__}"


async def _run(missing: list[int], concurrency: int) -> dict[str, int]:
    sem = asyncio.Semaphore(concurrency)
    stats: dict[str, int] = {}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def worker(iid: int):
            async with sem:
                _, status = await _download_one(session, iid)
                key = "ok" if status == "ok" else ("not_found" if status.startswith("http_404") else status)
                stats[key] = stats.get(key, 0) + 1
                if status != "ok":
                    print(f"  {iid}: {status}")
        await asyncio.gather(*(worker(iid) for iid in missing))
    return stats


async def _wiki_resolve(byid: dict[int, str], ids: list[int]) -> int:
    """Stage 3: resolve remaining ids to a canonical wiki id, fetch, and copy."""
    import osrs_api

    resolved = 0
    timeout = aiohttp.ClientTimeout(total=30)
    async with osrs_api.create_client() as client, aiohttp.ClientSession(timeout=timeout) as http:
        for iid in ids:
            name = byid[iid]
            try:
                canonical = await client.semantic.get_item_id(name)
            except Exception:
                canonical = None
            if not canonical:
                print(f"  {iid} {name!r}: no wiki id")
                continue
            src = item_image_path(canonical)
            if not os.path.exists(src):
                _, status = await _download_one(http, int(canonical))
                if status != "ok":
                    print(f"  {iid} {name!r}: canonical {canonical} icon {status}")
                    continue
            shutil.copyfile(src, item_image_path(iid))
            resolved += 1
    return resolved


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--no-wiki", action="store_true", help="skip stage 3 wiki resolution")
    args = ap.parse_args()

    byid = _all_items()
    missing = _missing(byid)
    print(f"Items missing an on-disk icon: {len(missing)}")
    if args.dry_run:
        print("dry-run: sample", missing[:20])
        return
    if not missing:
        print("Nothing to do.")
        return

    # Stage 1: direct RuneLite download.
    stats = asyncio.run(_run(missing, args.concurrency))
    print(f"Stage 1 (direct download): {stats.get('ok', 0)} ok, {stats.get('not_found', 0)} not found")

    # Stage 2: link placeholder/variant ids to a same-name base icon.
    linked = _link_placeholders(byid)
    print(f"Stage 2 (placeholder link): {linked} linked")

    # Stage 3: wiki-resolve whatever remains.
    remaining = _missing(byid)
    if remaining and not args.no_wiki:
        wiki = asyncio.run(_wiki_resolve(byid, remaining))
        print(f"Stage 3 (wiki resolve): {wiki} resolved")

    final = _missing(byid)
    print(f"Still missing after all stages: {len(final)}")
    if final:
        for iid in final:
            print(f"  {iid}: {byid[iid]!r}")


if __name__ == "__main__":
    main()
