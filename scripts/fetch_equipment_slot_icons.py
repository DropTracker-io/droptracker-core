"""Fetch the OSRS empty-equipment-slot tiles used by the website's gear panel.

The worn-equipment panel on a personal best draws each slot the way the game
does: a stone tile, with a faint glyph when the slot is empty and the item
sprite over a plain tile when it is not. Those tiles are the wiki's
``<Slot>_slot.png`` files — 36x36, the exact sprites the game interface uses.

``static/assets/img/*`` is gitignored (icons are data, not source), so this
script is how the assets get onto a box rather than committing binaries.

The plain tile has no wiki file of its own, so it is reconstructed: each of the
eleven tiles shares one stone background and differs only in its glyph, so the
per-pixel median across all eleven is the background with every glyph voted
out.

Usage:
    ./venv/bin/python3 -m scripts.fetch_equipment_slot_icons [--force]

Idempotent — existing files are left alone unless --force is passed.
"""

import argparse
import io
import os
import statistics
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.item_images import ensure_public_dir  # noqa: E402
from utils.wiki_ua import USER_AGENT  # noqa: E402  (one identity for the wiki)

OUT_DIR = "/store/droptracker/disc/static/assets/img/equipment"

WIKI_IMAGE_URL = "https://oldschool.runescape.wiki/images/{name}.png"

# The wiki's file name for each slot, keyed by the name the frontend asks for.
# The frontend names follow the game's own slot vocabulary; the wiki uses
# "Neck" for the amulet slot and "Feet" for boots.
SLOT_FILES = {
    "head": "Head_slot",
    "cape": "Cape_slot",
    "amulet": "Neck_slot",
    "ammo": "Ammo_slot",
    "weapon": "Weapon_slot",
    "body": "Body_slot",
    "shield": "Shield_slot",
    "legs": "Legs_slot",
    "hands": "Hands_slot",
    "feet": "Feet_slot",
    "ring": "Ring_slot",
}


def _write(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        os.chmod(tmp, 0o666)
    except OSError:
        pass
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="re-download tiles that already exist"
    )
    args = parser.parse_args()

    ensure_public_dir(OUT_DIR)

    from PIL import Image

    tiles = {}
    for slot, wiki_name in SLOT_FILES.items():
        path = os.path.join(OUT_DIR, f"{slot}.png")
        if os.path.exists(path) and not args.force:
            with open(path, "rb") as fh:
                tiles[slot] = fh.read()
            print(f"{slot}: already present")
            continue
        response = requests.get(
            WIKI_IMAGE_URL.format(name=wiki_name),
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if response.status_code != 200 or not response.content:
            print(f"{slot}: FAILED ({response.status_code})")
            continue
        _write(path, response.content)
        tiles[slot] = response.content
        print(f"{slot}: fetched {len(response.content)} bytes")

    blank_path = os.path.join(OUT_DIR, "blank.png")
    if len(tiles) < len(SLOT_FILES):
        print("\nnot all slot tiles available — skipping blank tile reconstruction")
        return 1
    if os.path.exists(blank_path) and not args.force:
        print("blank: already present")
        return 0

    images = [Image.open(io.BytesIO(b)).convert("RGBA") for b in tiles.values()]
    width, height = images[0].size
    pixels = [im.load() for im in images]
    blank = Image.new("RGBA", (width, height))
    out = blank.load()
    for y in range(height):
        for x in range(width):
            samples = [p[x, y] for p in pixels]
            # Median per channel: a glyph covers a given pixel in only a few of
            # the eleven tiles, so it never survives the vote.
            out[x, y] = tuple(
                int(statistics.median(s[c] for s in samples)) for c in range(4)
            )
    buffer = io.BytesIO()
    blank.save(buffer, "PNG")
    _write(blank_path, buffer.getvalue())
    print(f"blank: reconstructed {len(buffer.getvalue())} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
