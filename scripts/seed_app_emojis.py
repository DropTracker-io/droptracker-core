#!/usr/bin/env python3
"""
Seed the bot's UI emoji as Discord application emojis
=====================================================

Every custom emoji the bot posts used to live on a guild, which means posting
it anywhere else needs ``USE_EXTERNAL_EMOJIS`` in the destination channel. Where
that permission is missing the message degrades badly — the reader sees the raw
``<:supporter:1263827303712948304>`` instead of a glyph. Application emojis
belong to the app rather than to a server, so they render in every channel the
bot can already speak in, with no permission and no emoji-server to maintain.

This uploads the set in ``utils/app_emojis.SPECS`` and writes
``static/app_emojis.json``, which ``utils/app_emojis.py`` reads at runtime.

Art comes from the guild emoji each key is migrating away from, pulled straight
off ``cdn.discordapp.com`` by its old id — nothing to export by hand. A local
override wins when present: drop a file at ``static/emoji/<key>.png`` (or
``.gif``) to supply new art, or to supply art for a key whose original guild
emoji is already gone (``join``).

**Emojis are per application, and the DropTracker core bot and the Hall of Fame
bot are different applications.** ``services/hall_of_fame.py`` is loaded by both
processes, so both need the same key uploaded, and each gets its own id. Pick
the app with ``--profile`` (see ``utils.app_emojis.PROFILE_TOKENS``); the map
file holds every profile at once and a run only ever rewrites its own section.

Usage:
    python scripts/seed_app_emojis.py --dry-run              # list what would upload
    python scripts/seed_app_emojis.py                        # upload + write the map (core)
    python scripts/seed_app_emojis.py --profile hof          # the Hall of Fame app
    python scripts/seed_app_emojis.py --profile all          # every configured app
    python scripts/seed_app_emojis.py --verify               # report drift, change nothing
    python scripts/seed_app_emojis.py --prune                # delete emojis no key claims
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageSequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from interactions.api.http.http_client import HTTPClient  # noqa: E402
from interactions.client.utils.serializer import to_image_data  # noqa: E402

from utils.app_emojis import (  # noqa: E402
    MAP_PATH,
    PROFILE_TOKENS,
    SPECS,
    validate_specs,
)

CDN_EMOJI = "https://cdn.discordapp.com/emojis/{id}.{ext}"
USER_AGENT = "DropTracker/1.0 (+https://www.droptracker.io; application emoji sync)"

#: Where hand-supplied art overrides the CDN copy.
OVERRIDE_DIR = PROJECT_ROOT / "static" / "emoji"

#: Discord rejects an emoji upload over 256 KiB. The animated brand mark is
#: already past it at source size, so oversized art is shrunk rather than
#: failed — an emoji renders at ~32px inline regardless.
MAX_UPLOAD_BYTES = 256 * 1024

#: Sizes tried, largest first, when the source art is too big.
SHRINK_TO = (128, 96, 64, 48, 32)

#: Discord's per-app ceiling, shared with the ~270 rank emojis and the ~1000
#: item/NPC emojis already up there (see scripts/seed_rank_emojis.py and
#: scripts/seed_game_emojis.py).
APP_EMOJI_LIMIT = 2000

#: Name prefixes belonging to the *other* seeders on this application. This
#: registry's own keys are unprefixed, so "not ours" cannot be decided by a
#: prefix — only by excluding everyone else's.
OTHER_SET_PREFIXES = ("rank_", "item_", "npc_")


def _get(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _shrink(raw: bytes, animated: bool) -> bytes:
    """Re-encode ``raw`` until it fits :data:`MAX_UPLOAD_BYTES`, or give up.

    Returns the original bytes when they already fit, and the smallest attempt
    when nothing does — the caller reports the failure rather than uploading
    something Discord will reject.
    """
    if len(raw) <= MAX_UPLOAD_BYTES:
        return raw
    smallest = raw
    for size in SHRINK_TO:
        image = Image.open(io.BytesIO(raw))
        buffer = io.BytesIO()
        if animated:
            frames = [
                frame.convert("RGBA").resize((size, size), Image.LANCZOS)
                for frame in ImageSequence.Iterator(image)
            ]
            frames[0].save(
                buffer, format="GIF", save_all=True, append_images=frames[1:],
                loop=0, duration=image.info.get("duration", 60), disposal=2,
            )
        else:
            image.convert("RGBA").resize((size, size), Image.LANCZOS).save(buffer, format="PNG")
        candidate = buffer.getvalue()
        if len(candidate) < len(smallest):
            smallest = candidate
        if len(candidate) <= MAX_UPLOAD_BYTES:
            return candidate
    return smallest


def source_art(key: str) -> bytes:
    """The image to upload for ``key``.

    A file under ``static/emoji/`` wins; otherwise the guild emoji the key is
    migrating from, fetched by its old id. Raises when neither exists — that
    key simply keeps its Unicode fallback.
    """
    spec = SPECS[key]
    for extension in ("gif", "png"):
        override = OVERRIDE_DIR / f"{key}.{extension}"
        if override.exists():
            return _shrink(override.read_bytes(), spec.animated or extension == "gif")
    if not spec.legacy_id:
        raise FileNotFoundError(
            f"no art for {key!r}: its source emoji is gone and "
            f"{OVERRIDE_DIR / (key + '.png')} does not exist"
        )
    extension = "gif" if spec.animated else "png"
    try:
        raw = _get(CDN_EMOJI.format(id=spec.legacy_id, ext=extension))
    except urllib.error.HTTPError as exc:
        raise FileNotFoundError(
            f"no art for {key!r}: cdn returned {exc.code} for the source emoji "
            f"{spec.legacy_id} (deleted?); drop a file at "
            f"{OVERRIDE_DIR / (key + '.' + extension)} to supply it"
        ) from exc
    return _shrink(raw, spec.animated)


def read_map() -> dict:
    """The whole map file, so one profile's run keeps the others' sections."""
    try:
        with open(MAP_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_map(full: dict) -> None:
    os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
    with open(MAP_PATH, "w", encoding="utf-8") as fh:
        json.dump(
            {profile: dict(sorted(entries.items())) for profile, entries in sorted(full.items())},
            fh, indent=2,
        )
        fh.write("\n")


async def seed_profile(profile: str, token: str, args) -> int:
    """Upload the set to one application. Returns a process exit code."""
    http = HTTPClient()
    await http.login(token)
    try:
        app_id = (await http.get_current_bot_information())["id"]
        existing = {e["name"]: e for e in await http.get_application_emojis(app_id)}
        print(f"[{profile}] app {app_id} owns {len(existing)} emoji(s)")

        wanted = {key: spec.name for key, spec in SPECS.items()}
        missing = {k: n for k, n in wanted.items() if n not in existing}
        # Only ever consider emojis this registry is responsible for. The core
        # app also owns ~270 rank_* (scripts/seed_rank_emojis.py) and ~1000
        # item_*/npc_* (scripts/seed_game_emojis.py), each seeded from its own
        # source of truth. This set is the unprefixed remainder, so it has to
        # name every other namespace explicitly — a prefix added there and not
        # here means the next --prune here deletes that whole set.
        managed = set(wanted.values())
        stale = [
            name for name in existing
            if not name.startswith(OTHER_SET_PREFIXES) and name not in managed
        ]

        if args.verify:
            mapped = read_map().get(profile, {})
            print(f"  uploaded : {len(wanted) - len(missing)}/{len(wanted)}")
            print(f"  missing  : {sorted(missing) or 'none'}")
            print(f"  unmanaged: {sorted(stale) or 'none'}")
            print(f"  mapped   : {len(mapped)}/{len(wanted)} in {MAP_PATH}")
            drifted = [
                k for k, n in wanted.items()
                if n in existing and mapped.get(k) != _reference(existing[n])
            ]
            print(f"  drifted  : {sorted(drifted) or 'none'}")
            return 0

        if args.prune:
            for name in stale:
                await http.delete_application_emoji(app_id, existing[name]["id"])
                print(f"  deleted :{name}:")
                existing.pop(name, None)

        if len(existing) + len(missing) > APP_EMOJI_LIMIT:
            print(f"error: {len(existing)} + {len(missing)} exceeds the {APP_EMOJI_LIMIT} "
                  "app emoji limit", file=sys.stderr)
            return 1

        todo = sorted(missing.items())
        print(f"  {len(todo)} to upload")
        for index, (key, name) in enumerate(todo, 1):
            try:
                art = source_art(key)
            except (FileNotFoundError, OSError, urllib.error.URLError) as exc:
                # Not fatal: the key keeps its Unicode fallback and shows up as
                # missing on the next --verify.
                print(f"  [{index}/{len(todo)}] SKIP {name}: {exc}", file=sys.stderr)
                continue
            if args.dry_run:
                print(f"  [{index}/{len(todo)}] would upload :{name}: ({len(art)} bytes)")
                continue
            if len(art) > MAX_UPLOAD_BYTES:
                print(f"  [{index}/{len(todo)}] SKIP {name}: {len(art)} bytes still exceeds "
                      f"the {MAX_UPLOAD_BYTES} byte limit after shrinking", file=sys.stderr)
                continue
            try:
                created = await http.create_application_emoji(
                    {"name": name, "image": to_image_data(art)}, app_id
                )
                existing[name] = created
                print(f"  [{index}/{len(todo)}] :{name}: -> {created['id']} ({len(art)} bytes)")
            except Exception as exc:  # noqa: BLE001 — one bad upload must not abort the rest
                print(f"  [{index}/{len(todo)}] FAILED {name}: {exc}", file=sys.stderr)

        if args.dry_run:
            return 0

        entries = {
            key: _reference(existing[name])
            for key, name in wanted.items() if name in existing
        }
        full = read_map()
        full[profile] = entries
        write_map(full)
        print(f"  wrote {len(entries)}/{len(wanted)} entries for {profile} to {MAP_PATH}")
        unresolved = sorted(set(wanted) - set(entries))
        if unresolved:
            print(f"  still on the unicode fallback: {unresolved}")
        return 0
    finally:
        await http.close()


def _reference(emoji: dict) -> str:
    """An emoji object from the API -> the ``<:name:id>`` a message embeds."""
    return f"<{'a' if emoji.get('animated') else ''}:{emoji['name']}:{emoji['id']}>"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--profile", default="core",
                        help=f"application to seed: {', '.join(PROFILE_TOKENS)}, or 'all'")
    parser.add_argument("--dry-run", action="store_true", help="list work, upload nothing")
    parser.add_argument("--verify", action="store_true", help="report drift, change nothing")
    parser.add_argument("--prune", action="store_true",
                        help="delete app emojis no key claims (rank_* are left alone)")
    args = parser.parse_args()

    problems = validate_specs()
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    if args.profile == "all":
        profiles = list(PROFILE_TOKENS)
    elif args.profile in PROFILE_TOKENS:
        profiles = [args.profile]
    else:
        print(f"error: unknown profile {args.profile!r}; "
              f"expected one of {', '.join(PROFILE_TOKENS)} or 'all'", file=sys.stderr)
        return 2

    worst = 0
    for profile in profiles:
        token = os.getenv(PROFILE_TOKENS[profile])
        if not token:
            message = f"{PROFILE_TOKENS[profile]} not set"
            # Asking for every profile on a box that only configures some is
            # normal; asking for one by name and not having it is an error.
            if args.profile == "all":
                print(f"[{profile}] skipped: {message}")
                continue
            print(f"error: {message}", file=sys.stderr)
            return 2
        worst = max(worst, await seed_profile(profile, token, args))
    return worst


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
