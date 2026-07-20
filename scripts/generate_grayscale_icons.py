#!/usr/bin/env python3
"""Backfill grayscale variants of every item icon.

The Loot Sweep board renders hundreds of "not yet received" receipt tabs as
greyed item icons. It used to grey them with a per-element CSS
``filter: grayscale(100%)``, which the browser re-rasterises every time a row
scrolls into view — the desktop scroll-jank culprit. Instead we serve a
pre-baked grayscale PNG at ``itemdb/gray/{id}.png`` and drop the filter.

This backfills the whole ``static/assets/img/itemdb/`` directory. It is
idempotent (skips variants that already exist) and safe to re-run. New icons
self-heal on demand via ``utils.item_images.ensure_grayscale_variant`` (called
from ``web/front.py``'s image route), so this only needs a periodic top-up.

Usage:
    venv/bin/python scripts/generate_grayscale_icons.py            # backfill all
    venv/bin/python scripts/generate_grayscale_icons.py --limit 20 # smoke test
    venv/bin/python scripts/generate_grayscale_icons.py --force    # regenerate all
"""
import argparse
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.item_images import (  # noqa: E402
    ITEMDB_DIR,
    GRAY_DIR,
    ensure_grayscale_variant,
    gray_image_path,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="regenerate even if the variant exists")
    ap.add_argument("--limit", type=int, default=0, help="process at most N icons (0 = all)")
    args = ap.parse_args()

    os.makedirs(GRAY_DIR, exist_ok=True)
    srcs = sorted(glob.glob(os.path.join(ITEMDB_DIR, "*.png")))
    made = skipped = failed = 0
    t0 = time.time()

    for src in srcs:
        if args.limit and (made + skipped + failed) >= args.limit:
            break
        stem = os.path.basename(src)[:-4]
        if not stem.isdigit():
            continue
        dst = gray_image_path(stem)
        if os.path.exists(dst):
            if not args.force:
                skipped += 1
                continue
            os.remove(dst)
        if ensure_grayscale_variant(stem):
            made += 1
        else:
            failed += 1
        n = made + skipped + failed
        if n % 2000 == 0:
            print(f"... {made} made, {skipped} skipped, {failed} failed ({time.time() - t0:.0f}s)")

    print(
        f"DONE: {made} made, {skipped} skipped, {failed} failed "
        f"of {len(srcs)} source icons ({time.time() - t0:.0f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
