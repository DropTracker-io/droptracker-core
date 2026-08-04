"""Backfill missing/inconsistent Hall of Fame boss (NPC) icons.

Ensures every row in `npc_list` has an on-disk icon at
``static/assets/img/npcdb/{npc_id}.png``, and that every *existing* icon is
normalized to the same canonical proportions the Hall of Fame renderer now
expects (see `utils.format.NPC_IMG_CANONICAL_SIZE`), in three idempotent
stages:

  1. Re-normalize every already-downloaded icon in place (contain-fit onto a
     transparent NPC_IMG_CANONICAL_SIZE canvas) — old icons were saved at
     native wiki aspect ratio (280px wide, 174-688px+ tall) and render as
     inconsistently-sized HOF thumbnails.
  2. Direct download: fetch the OSRS Wiki thumbnail for any npc_id still
     missing an icon (`utils.format.get_npc_image_url`, which already
     normalizes on save).
  3. Raid-mode fallback: for any npc still missing after stage 2 (uncommon —
     usually a raid-mode variant whose wiki page has no distinct thumbnail),
     copy the icon of another mode of the same raid, preferring the
     base/normal mode (`utils.hof.RAID_GROUPS`).

Safe to re-run: stage 1 is idempotent (re-normalizing an already-canonical
image is a no-op fit), stage 2 skips ids that already have a file, stage 3
only touches ids still missing after stage 2.

Usage:
    ./venv/bin/python3 -m scripts.backfill_npc_images [--dry-run] [--concurrency N] [--skip-renormalize]
"""

import argparse
import asyncio
import os
import sys

import pymysql
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402,F401  (import before utils.format to avoid a circular-import ordering issue)
from utils.format import NPC_IMG_DIR, get_npc_image_url, normalize_npc_image  # noqa: E402
from utils.hof import RAID_GROUPS, canonical_display_name, npc_name_candidates  # noqa: E402


def _npc_image_path(npc_id: int) -> str:
    return f"{NPC_IMG_DIR}/{npc_id}.png"


def _all_npcs() -> dict[int, str]:
    conn = pymysql.connect(
        host="localhost",
        port=3306,
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        database="data",
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT npc_id, npc_name FROM npc_list")
        return {i: n for i, n in cur.fetchall() if i is not None}
    finally:
        conn.close()


def _missing(byid: dict[int, str]) -> list[int]:
    return sorted(i for i in byid if not os.path.exists(_npc_image_path(i)))


def _renormalize_existing(byid: dict[int, str]) -> tuple[int, int]:
    """Stage 1: re-fit every on-disk icon onto the canonical canvas."""
    fixed = 0
    failed = 0
    for npc_id in byid:
        path = _npc_image_path(npc_id)
        if not os.path.exists(path):
            continue
        try:
            with Image.open(path) as image:
                normalized = normalize_npc_image(image)
            tmp = f"{path}.tmp.{os.getpid()}"
            normalized.save(tmp, "PNG")
            os.replace(tmp, path)
            fixed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  {npc_id}: renormalize failed ({type(e).__name__}: {e})")
            failed += 1
    return fixed, failed


async def _download_one(npc_id: int, npc_name: str, sem: asyncio.Semaphore) -> tuple[int, str]:
    async with sem:
        try:
            url = await get_npc_image_url(npc_name, npc_id)
            return npc_id, "ok" if url else "not_found"
        except Exception as e:  # noqa: BLE001
            return npc_id, f"error:{type(e).__name__}"


async def _run_downloads(byid: dict[int, str], missing: list[int], concurrency: int) -> dict[str, int]:
    sem = asyncio.Semaphore(concurrency)
    stats: dict[str, int] = {}
    results = await asyncio.gather(*(_download_one(i, byid[i], sem) for i in missing))
    for npc_id, status in results:
        stats[status] = stats.get(status, 0) + 1
        if status != "ok":
            print(f"  {npc_id} {byid[npc_id]!r}: {status}")
    return stats


def _raid_fallback_link(byid: dict[int, str], ids: list[int]) -> int:
    """Stage 3: copy another mode's icon (base/normal mode preferred) onto a
    still-missing raid-mode variant."""
    name_to_id = {name: npc_id for npc_id, name in byid.items()}
    linked = 0
    for npc_id in ids:
        npc_name = byid[npc_id]
        canonical = canonical_display_name(npc_name)
        variants = RAID_GROUPS.get(canonical)
        if not variants:
            continue
        source_path = None
        for variant_name in variants:
            for candidate in npc_name_candidates(variant_name):
                cand_id = name_to_id.get(candidate)
                if cand_id is None or cand_id == npc_id:
                    continue
                cand_path = _npc_image_path(cand_id)
                if os.path.exists(cand_path):
                    source_path = cand_path
                    break
            if source_path:
                break
        if source_path:
            dest = _npc_image_path(npc_id)
            with open(source_path, "rb") as src_f, open(f"{dest}.tmp.{os.getpid()}", "wb") as dst_f:
                dst_f.write(src_f.read())
            os.replace(f"{dest}.tmp.{os.getpid()}", dest)
            linked += 1
        else:
            print(f"  {npc_id} {npc_name!r}: no sibling raid-mode icon available either")
    return linked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--skip-renormalize", action="store_true", help="skip stage 1 (re-fit existing icons)")
    args = ap.parse_args()

    byid = _all_npcs()
    missing = _missing(byid)
    print(f"NPCs tracked: {len(byid)}, missing an on-disk icon: {len(missing)}")
    if args.dry_run:
        print("dry-run: sample missing", missing[:20])
        return

    if not args.skip_renormalize:
        fixed, failed = _renormalize_existing(byid)
        print(f"Stage 1 (re-normalize existing icons): {fixed} fixed, {failed} failed")

    if missing:
        stats = asyncio.run(_run_downloads(byid, missing, args.concurrency))
        print(f"Stage 2 (direct wiki download): {stats.get('ok', 0)} ok, "
              f"{sum(v for k, v in stats.items() if k != 'ok')} failed")

    remaining = _missing(byid)
    if remaining:
        linked = _raid_fallback_link(byid, remaining)
        print(f"Stage 3 (raid-mode fallback link): {linked} linked")

    final = _missing(byid)
    print(f"Still missing after all stages: {len(final)}")
    if final:
        for npc_id in final:
            print(f"  {npc_id}: {byid[npc_id]!r}")


if __name__ == "__main__":
    main()
