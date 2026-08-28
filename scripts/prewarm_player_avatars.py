"""Pre-generate the torso-up avatar crops the website draws in listings.

The crop is derived on demand by the image server, so nothing *needs* this: a
player whose avatar has never been asked for gets one built on the first
request. This exists for the two cases where on-demand is the wrong moment.

* **The 2.6k outfits that predate the feature.** Without a pass like this, the
  first person to open a leaderboard pays for every crop on it.
* **Where the derivation runs.** The image server lives inside the core bot
  process, and while each crop is handed to a thread, a burst of them is still
  work happening next to the Discord gateway. Cheaper to do it here, once.

Usage:
    ./venv/bin/python3 -m scripts.prewarm_player_avatars [--apply] [--limit N]

Dry-run by default, like every maintenance script here. Idempotent: an outfit
that already has a crop is skipped after one ``stat``, so re-running costs
almost nothing and is the intended way to recover after a prune.

Run it as the account that owns the image tree (``user``), not as ``debian`` —
crops written by the wrong account are the permission trap this repo has hit
before. The files are written 0666 either way, but the *directory* has to have
been created by someone both accounts can write through.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually write crops")
    parser.add_argument("--limit", type=int, default=0, help="stop after N players")
    args = parser.parse_args()

    from db.models import PlayerState, Session
    from services.gear_image import image_exists
    from services.player_avatar import (
        avatar_exists,
        build_avatar,
        ensure_avatar,
    )
    from services.gear_image import image_path

    session = Session()
    try:
        rows = (
            session.query(
                PlayerState.player_id,
                PlayerState.model_fingerprint,
                PlayerState.pinned_model_fingerprint,
            )
            .filter(PlayerState.model_fingerprint.isnot(None))
            .all()
        )
    finally:
        session.close()

    considered = built = skipped = no_render = unusable = failed = 0
    for player_id, current, pinned in rows:
        # Mirrors what the image server resolves for `avatar.png`: the pinned
        # outfit is the one the player chose, so it is the one worth warming.
        fingerprint = pinned or current
        if not fingerprint:
            continue
        considered += 1
        if avatar_exists(player_id, fingerprint):
            skipped += 1
            continue
        if not image_exists(player_id, fingerprint):
            # The model is on record but its render is not on disk. Nothing to
            # crop; the site falls back to the letter tile, which is correct.
            no_render += 1
            continue
        if not args.apply:
            built += 1
            continue
        if ensure_avatar(player_id, fingerprint):
            built += 1
        # A render the crop refuses to frame is not a failure — some stored
        # screenshots are partial or empty, and declining to crop one is the
        # designed outcome (the site shows the letter tile). Only a render that
        # *does* yield a crop and still did not land is a real failure, so the
        # two are counted apart: conflating them hides a broken image tree
        # behind a pile of expected rejections.
        elif build_avatar(image_path(player_id, fingerprint)) is None:
            unusable += 1
        else:
            failed += 1
        if args.limit and built >= args.limit:
            break

    verb = "would build" if not args.apply else "built"
    print(
        f"{considered} players with a model: {verb} {built}, "
        f"{skipped} already cached, {no_render} with no render, "
        f"{unusable} with an unusable render, {failed} failed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
