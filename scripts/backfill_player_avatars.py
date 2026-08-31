"""Render the avatar crop for character models that already have a still.

The avatar — the square head-and-shoulders picture the site draws instead of a
letter tile — is rendered when the plugin uploads an outfit. Models uploaded
before that existed have a full-body still and no avatar, so their owners would
keep the letter tile until they next change gear. This renders the missing ones.

Walks the model tree rather than the database on purpose: the filenames *are*
the index (one `{fingerprint}.glb` per outfit), and a player who has changed
gear since has older outfits on disk that no row points at any more. Backfilling
those too costs one render and means an avatar is already waiting if they ever
wear that set again.

Renders are serial. Each one is a headless chromium drawing WebGL on the CPU,
and the box this runs on is also serving the site.

Run it *after* the web app that draws the crop is deployed. The render page is
part of droptracker-web, and a server still on an older build answers
``?avatar=1`` with a full-body picture. That cannot be stored by accident — the
readiness probe checks which framing was actually drawn — but every outfit will
fail until the right build is live.

Safe to re-run: an outfit that already has an avatar is skipped without
launching a browser.

Usage:
    ./venv/bin/python3 -m scripts.backfill_player_avatars [--dry-run] [--limit N]
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402,F401  (import first: see scripts/backfill_npc_images.py)
from services.gear_image import (  # noqa: E402
    IMAGE_ROOT,
    avatar_exists,
    render_avatar_image,
)
from services.player_model import is_valid_fingerprint  # noqa: E402


def _outfits():
    """Every (player_id, fingerprint) with a stored model, oldest file first.

    Pet models share the tree as `{fingerprint}-pet.glb`; they are not outfits
    and `is_valid_fingerprint` rejects the suffixed name, which is what keeps
    them out.
    """
    if not os.path.isdir(IMAGE_ROOT):
        return []

    found = []
    for entry in sorted(os.listdir(IMAGE_ROOT)):
        directory = os.path.join(IMAGE_ROOT, entry)
        if not entry.isdigit() or not os.path.isdir(directory):
            continue
        player_id = int(entry)
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".glb"):
                continue
            fingerprint = name[: -len(".glb")]
            if not is_valid_fingerprint(fingerprint):
                continue
            found.append((player_id, fingerprint))
    return found


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be rendered and exit.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after this many renders (0 = no limit).")
    args = parser.parse_args()

    outfits = _outfits()
    pending = [(pid, fp) for pid, fp in outfits if not avatar_exists(pid, fp)]
    print(f"{len(outfits)} stored outfit(s); {len(pending)} without an avatar.")

    if args.dry_run:
        for player_id, fingerprint in pending:
            print(f"  would render {player_id}/{fingerprint}")
        return 0

    if args.limit > 0:
        pending = pending[: args.limit]

    rendered = failed = 0
    for player_id, fingerprint in pending:
        url = await render_avatar_image(player_id, fingerprint)
        if url:
            rendered += 1
            print(f"  {player_id}/{fingerprint} -> {url}")
        else:
            failed += 1
            print(f"  {player_id}/{fingerprint} FAILED")

    print(f"Rendered {rendered}, failed {failed}.")
    # A failed render is worth a non-zero exit: it usually means the web app is
    # not reachable, and every remaining outfit would fail the same way.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
