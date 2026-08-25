#!/usr/bin/env python3
"""
Seed the item and NPC set as Discord application emojis
=======================================================

``scripts/rank_game_emojis.py`` decides *which* ~1000 items and NPCs are worth a
glyph and writes ``data/game_emojis.json``. This uploads them and writes
``static/game_emojis.json``, the map ``utils/game_emojis.py`` reads at runtime.

**Application** emojis, not guild emojis, for the same reason the UI and rank
sets moved (see ``utils/app_emojis.py``): posting a guild emoji costs the
sending bot ``USE_EXTERNAL_EMOJIS`` in the destination channel, and where that
permission is missing the message does not degrade — the reader sees the raw
``<:item_twisted_bow:123>``. Application emojis need no permission, work in
every channel the bot can already speak in, and cap at 2000 per app rather than
250 per server. With 270 ``rank_*`` and 8 UI emojis already up, a 1000-entry set
leaves ~720 spare.

Art comes off this box: ``static/assets/img/itemdb/{id}.png`` (RuneLite's cache,
backfilled for the whole catalogue) and ``static/assets/img/npcdb/{id}.png``
(wiki renders). Nothing is fetched from the network here — a manifest entry
whose art is missing was already rejected by the ranking script.

The two source sets are different shapes and need opposite treatment. Item icons
are 36x32 pixel art, upscaled nearest-neighbour so Discord's own scale-up to
inline text height stays crisp instead of smearing. NPC renders are 280x280
photographs of a model, downscaled with LANCZOS — they carry no pixel grid to
preserve and the full size is 50 KB of detail nobody sees at 22 px.

**A run of 1000 uploads is long.** Discord rate-limits emoji creation hard and
the client backs off through it, so expect this to take a while. It is
idempotent — an emoji that already exists by name is adopted, not re-uploaded —
so ``--limit`` lets it be done in sittings and a killed run loses nothing.

Usage:
    python scripts/seed_game_emojis.py --dry-run          # list what would upload
    python scripts/seed_game_emojis.py --limit 50         # a first sitting
    python scripts/seed_game_emojis.py                    # the rest (core)
    python scripts/seed_game_emojis.py --profile all      # every configured app
    python scripts/seed_game_emojis.py --verify           # report drift, change nothing
    python scripts/seed_game_emojis.py --prune            # delete item_*/npc_* no entry claims
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from interactions.api.http.http_client import HTTPClient  # noqa: E402
from interactions.client.utils.serializer import to_image_data  # noqa: E402

from utils.game_emojis import (  # noqa: E402
    ITEM_PREFIX,
    MAP_PATH,
    NPC_PREFIX,
    PROFILE_TOKENS,
    art_path_for,
    load_manifest,
    manifest_entries,
    validate_manifest,
)

#: Discord rejects an upload over 256 KiB. Neither source set comes close after
#: the resize below, but the check stays so a future art source cannot fail the
#: whole run one file at a time.
MAX_UPLOAD_BYTES = 256 * 1024

#: 36x32 item sprites -> 144x128. Nearest-neighbour: these are pixel art and
#: interpolating them turns a crisp 4-pixel hilt into a grey smudge.
ITEM_UPSCALE = 4

#: 280x280 NPC renders -> 128x128. Discord shows an emoji at 22-48 px, so the
#: rest is payload nobody sees; LANCZOS because these are smooth renders.
NPC_SIZE = 128

#: Discord's per-app ceiling, shared with rank_* and the UI keys.
APP_EMOJI_LIMIT = 2000

#: Only these two namespaces belong to this seeder. --prune must never touch
#: `rank_*` (scripts/seed_rank_emojis.py) or the unprefixed UI keys
#: (scripts/seed_app_emojis.py), and theirs must never touch these.
MANAGED_PREFIXES = (ITEM_PREFIX, NPC_PREFIX)


def render(entry: dict) -> bytes:
    """One manifest entry's art, sized for Discord."""
    path = art_path_for(entry)
    image = Image.open(path).convert("RGBA")
    if entry["kind"] == "item":
        image = image.resize(
            (image.width * ITEM_UPSCALE, image.height * ITEM_UPSCALE), Image.NEAREST
        )
    elif max(image.size) > NPC_SIZE:
        image.thumbnail((NPC_SIZE, NPC_SIZE), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def read_map() -> dict:
    """The whole map file, so one profile's run keeps the others' sections."""
    try:
        data = json.loads(Path(MAP_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_map(full: dict) -> None:
    os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
    with open(MAP_PATH, "w", encoding="utf-8") as fh:
        json.dump(
            {
                profile: {kind: dict(sorted(entries.items()))
                          for kind, entries in sorted(kinds.items())}
                for profile, kinds in sorted(full.items())
            },
            fh, indent=2, ensure_ascii=False,
        )
        fh.write("\n")


def _reference(emoji: dict) -> str:
    """An emoji object from the API -> the ``<:name:id>`` a message embeds."""
    return f"<{'a' if emoji.get('animated') else ''}:{emoji['name']}:{emoji['id']}>"


def _sections(entries: list, existing: dict) -> dict:
    """``{kind: {key: "<:name:id>"}}`` for everything the app actually owns."""
    sections: dict = {"item": {}, "npc": {}}
    for entry in entries:
        owned = existing.get(entry["emoji"])
        if owned:
            sections[entry["kind"]][entry["key"]] = _reference(owned)
    return sections


async def seed_profile(profile: str, token: str, args) -> int:
    """Upload the set to one application. Returns a process exit code."""
    entries = manifest_entries()
    http = HTTPClient()
    await http.login(token)
    try:
        app_id = (await http.get_current_bot_information())["id"]
        existing = {e["name"]: e for e in await http.get_application_emojis(app_id)}
        wanted = {entry["emoji"]: entry for entry in entries}
        missing = [entry for name, entry in wanted.items() if name not in existing]
        stale = [name for name in existing
                 if name.startswith(MANAGED_PREFIXES) and name not in wanted]
        print(f"[{profile}] app {app_id} owns {len(existing)} emoji(s); "
              f"manifest wants {len(wanted)}, {len(missing)} not up yet")

        if args.verify:
            mapped = read_map().get(profile, {})
            up = len(wanted) - len(missing)
            print(f"  uploaded : {up}/{len(wanted)}")
            print(f"  missing  : {len(missing)}"
                  + (f" (e.g. {[e['emoji'] for e in missing[:5]]})" if missing else ""))
            print(f"  stale    : {len(stale)}" + (f" (e.g. {stale[:5]})" if stale else ""))
            print(f"  mapped   : {sum(len(v) for v in mapped.values())}/{len(wanted)} in {MAP_PATH}")
            drifted = [name for name, entry in wanted.items()
                       if name in existing
                       and (mapped.get(entry["kind"]) or {}).get(entry["key"])
                       != _reference(existing[name])]
            print(f"  drifted  : {len(drifted)}" + (f" (e.g. {drifted[:5]})" if drifted else ""))
            return 0

        if args.prune:
            for name in stale:
                await http.delete_application_emoji(app_id, existing[name]["id"])
                print(f"  deleted :{name}:")
                existing.pop(name, None)

        if len(existing) + len(missing) > APP_EMOJI_LIMIT:
            print(f"error: {len(existing)} already up + {len(missing)} to upload exceeds the "
                  f"{APP_EMOJI_LIMIT} app emoji limit. Lower --budget in "
                  f"scripts/rank_game_emojis.py and rerun it.", file=sys.stderr)
            return 1

        todo = sorted(missing, key=lambda e: e["emoji"])
        if args.limit:
            todo = todo[:args.limit]
        print(f"  {len(todo)} to upload"
              f"{f' (capped from {len(missing)} by --limit)' if args.limit else ''}")

        failures = 0
        for index, entry in enumerate(todo, 1):
            label = f"[{index}/{len(todo)}] :{entry['emoji']}:"
            try:
                art = render(entry)
            except (OSError, ValueError) as exc:
                print(f"  {label} SKIP — unreadable art {art_path_for(entry)}: {exc}",
                      file=sys.stderr)
                failures += 1
                continue
            if len(art) > MAX_UPLOAD_BYTES:
                print(f"  {label} SKIP — {len(art)} bytes exceeds the {MAX_UPLOAD_BYTES} "
                      f"byte limit", file=sys.stderr)
                failures += 1
                continue
            if args.dry_run:
                print(f"  {label} would upload {entry['name']!r} ({len(art)} bytes)")
                continue
            try:
                created = await http.create_application_emoji(
                    {"name": entry["emoji"], "image": to_image_data(art)}, app_id
                )
                existing[entry["emoji"]] = created
                print(f"  {label} -> {created['id']} ({entry['name']}, {len(art)} bytes)")
            except Exception as exc:  # noqa: BLE001 — one bad upload must not abort 999
                print(f"  {label} FAILED: {exc}", file=sys.stderr)
                failures += 1
            # Write through every so often: a 1000-upload run is long enough
            # that being interrupted with nothing recorded would mean the map
            # and the app disagree until the next full run.
            if not args.dry_run and index % 50 == 0:
                full = read_map()
                full[profile] = _sections(entries, existing)
                write_map(full)

        if args.dry_run:
            return 0

        full = read_map()
        full[profile] = _sections(entries, existing)
        write_map(full)
        resolved = sum(len(section) for section in full[profile].values())
        print(f"  wrote {resolved}/{len(wanted)} entries for {profile} to {MAP_PATH}")
        if resolved < len(wanted):
            print(f"  {len(wanted) - resolved} still unseeded — rerun to fill the gaps")
        return 1 if failures else 0
    finally:
        await http.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--profile", default="core",
                        help=f"application to seed: {', '.join(PROFILE_TOKENS)}, or 'all'")
    parser.add_argument("--dry-run", action="store_true", help="list work, upload nothing")
    parser.add_argument("--verify", action="store_true", help="report drift, change nothing")
    parser.add_argument("--prune", action="store_true",
                        help="delete item_*/npc_* emojis the manifest no longer claims")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap uploads this run (0 = no cap); reruns resume")
    args = parser.parse_args()

    manifest = load_manifest()
    if not manifest["items"] and not manifest["npcs"]:
        print("error: the manifest is empty — run scripts/rank_game_emojis.py --write first",
              file=sys.stderr)
        return 2
    problems = validate_manifest()
    if problems:
        for problem in problems[:20]:
            print(f"error: {problem}", file=sys.stderr)
        if len(problems) > 20:
            print(f"error: ... and {len(problems) - 20} more", file=sys.stderr)
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
