"""Redraw character renders that the old pet placement shrank to specks.

Until web a0ee557 the character renderer stood a pet twenty tiles from its
player (a gap written in game units but applied in tiles), and framing both
drew them as two specks at opposite edges of the frame. Every render made
while a pet was out has that shape. A render is drawn once per outfit and
then kept, so these stay wrong after the fix until they are drawn again: the
Discord still, and the image the avatar crop is cut from. The crop refuses to
frame a speck, so those players show the letter placeholder.

A speck is not always an outfit that has a pet *now*. The fingerprint names
the player's outfit, not their follower, so an outfit first stored with a pet
out and stored again later without one keeps its old render.

What this redraws: every stored render that is a speck and whose model is
still stored. The render page draws from the model, so a speck whose model
has been pruned cannot be redrawn and is left alone. Specks are found by size
first (nearly all transparency: 6-11 KB, against 32 KB and up for a real
render), then confirmed by measuring the figure before anything is redrawn.
The redraw uses what the outfit holds now: with its pet if the pet model is
stored, alone if not. Current and pinned outfits go first, because those are
what profiles, avatars and new notifications show.

Run it only once the fixed renderer is deployed to the page renders are
taken from (``WEB_BASE_URL``, default blue on :31380). It checks for itself:
every redraw is measured again, and the run stops at the first one that is
still a speck.

Usage:
    ./venv/bin/python3 -m scripts.rerender_model_specks [--apply] [--limit N] [--pause S]

Dry-run by default, like every maintenance script here. Idempotent: a redrawn
render is no longer a speck, so a re-run passes over it. Each redraw is a
headless chromium screenshot on a box short of CPU, so they run one at a time
with a pause between them.

In B2 mode (production) the CDN may keep serving an old render it has cached
for up to its max-age (7 days). The avatar crop reads the bucket directly, so
avatars are unaffected.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A real render is 32 KB and up; a speck is nearly all transparency, 6-11 KB
# (376 current renders measured, 2026-09-10). Only renders under this are
# downloaded and measured, so finding them costs one bucket listing rather
# than a download per render.
SPECK_MAX_BYTES = 20_000

# The avatar crop's own sanity bar: a figure under a fifth of the frame's
# height is not a character drawn at the fixed camera's scale.
SPECK_MAX_FIGURE = 0.20

_RENDER_RE = re.compile(r"^([0-9a-f]{1,32})\.png$")


def figure_fraction(png: bytes) -> float:
    """Height of everything drawn, as a fraction of the frame (0 if empty)."""
    import numpy as np
    from PIL import Image

    from services.player_avatar import _ALPHA_FLOOR

    img = Image.open(io.BytesIO(png))
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    solid = np.asarray(img.split()[-1]) > _ALPHA_FLOOR
    rows = np.flatnonzero(solid.any(axis=1))
    if rows.size == 0:
        return 0.0
    return float(rows[-1] - rows[0]) / solid.shape[0]


def _player_dirs():
    """Yields ``(player_id, {filename: size})`` for every player's model dir."""
    from services.player_model import MODEL_ROOT, _b2, _b2_enabled

    if _b2_enabled():
        b2 = _b2()
        prefix = f"{b2.MODELS_PREFIX}/"
        current, files = None, {}
        # Keys come back in lexicographic order, so one player's are adjacent.
        for item in b2.list_keys(prefix):
            parts = item["key"][len(prefix):].split("/")
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            if parts[0] != current:
                if current is not None:
                    yield int(current), files
                current, files = parts[0], {}
            files[parts[1]] = item["size"]
        if current is not None:
            yield int(current), files
        return

    if not os.path.isdir(MODEL_ROOT):
        return
    for entry in os.scandir(MODEL_ROOT):
        if not (entry.is_dir() and entry.name.isdigit()):
            continue
        files = {}
        for f in os.scandir(entry.path):
            if f.is_file():
                files[f.name] = f.stat().st_size
        yield int(entry.name), files


def _read_render(player_id: int, fingerprint: str):
    from services.gear_image import image_key, image_path
    from services.player_model import _b2, _b2_enabled

    if _b2_enabled():
        return _b2().get_bytes(image_key(player_id, fingerprint))
    try:
        with open(image_path(player_id, fingerprint), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _drop_avatar(player_id: int, fingerprint: str) -> None:
    """Deletes a crop cut from the old render, so the next request recuts it.

    A speck is normally refused by the crop, so there is rarely one to delete.
    """
    from services.player_avatar import avatar_key, avatar_path
    from services.player_model import _b2, _b2_enabled

    if _b2_enabled():
        b2 = _b2()
        if b2.key_exists(avatar_key(player_id, fingerprint), use_cache=False):
            b2.delete_key(avatar_key(player_id, fingerprint))
        return
    try:
        os.unlink(avatar_path(player_id, fingerprint))
    except OSError:
        pass


def _current_outfits() -> set:
    from db.models import PlayerState, Session

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
    out = set()
    for player_id, current, pinned in rows:
        out.update((player_id, fp) for fp in (current, pinned) if fp)
    return out


async def _run(args) -> int:
    from services.gear_image import render_gear_image

    listed = 0
    candidates = []  # (player_id, fingerprint, has_pet_now)
    for player_id, files in _player_dirs():
        for name, size in files.items():
            match = _RENDER_RE.match(name)
            if not match:
                continue
            listed += 1
            fingerprint = match.group(1)
            if size < SPECK_MAX_BYTES and f"{fingerprint}.glb" in files:
                candidates.append(
                    (player_id, fingerprint, f"{fingerprint}-pet.glb" in files))

    current = _current_outfits()
    candidates.sort(key=lambda c: (c[:2] not in current, c[0], c[1]))
    print(f"{listed} renders stored; {len(candidates)} under "
          f"{SPECK_MAX_BYTES // 1000} KB with their model still stored")

    confirmed = redrawn = failed = not_speck = unreadable = 0
    confirmed_current = confirmed_with_pet = 0
    for player_id, fingerprint, has_pet in candidates:
        if args.limit and confirmed >= args.limit:
            break
        png = _read_render(player_id, fingerprint)
        if png is None:
            unreadable += 1
            continue
        if figure_fraction(png) >= SPECK_MAX_FIGURE:
            # Small but a real figure (a slim character, little colour).
            not_speck += 1
            continue
        confirmed += 1
        confirmed_current += (player_id, fingerprint) in current
        confirmed_with_pet += has_pet
        tag = (f"player {player_id} outfit {fingerprint}"
               f"{' (current)' if (player_id, fingerprint) in current else ''}"
               f"{' with pet' if has_pet else ''}")
        if not args.apply:
            if confirmed <= 15:
                print(f"  would redraw {tag}")
            continue

        url = await render_gear_image(player_id, fingerprint, force=True)
        redone = _read_render(player_id, fingerprint) if url else None
        if redone is None:
            failed += 1
            print(f"  FAILED to redraw {tag}")
            continue
        if figure_fraction(redone) < SPECK_MAX_FIGURE:
            print(f"  STOPPING: the redraw of {tag} is still a speck. The page at "
                  f"WEB_BASE_URL is not serving the fixed renderer yet; deploy "
                  f"web a0ee557 or later first.")
            return 1
        _drop_avatar(player_id, fingerprint)
        redrawn += 1
        print(f"  redrew {tag}")
        time.sleep(args.pause)

    if args.apply:
        print(f"redrew {redrawn} of {confirmed} specks ({failed} failed); ", end="")
    else:
        print(f"would redraw {confirmed} specks; ", end="")
    print(f"{confirmed_current} of them current or pinned outfits, "
          f"{confirmed_with_pet} with a pet stored now. {not_speck} small "
          f"renders were real figures; {unreadable} could not be read.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually redraw")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N confirmed specks")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="seconds to wait between redraws (default 1)")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
