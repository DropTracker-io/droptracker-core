#!/usr/bin/env python3
"""
Seed OSRS clan rank icons as Discord application emojis
=======================================================

The clan chat bridge renders mirror lines as ``:rank: **Name**: message``.
Discord has no inline-image primitive — an embed author icon or a Components V2
thumbnail sits *beside* a line, never inside it — so the rank glyph has to be a
real custom emoji. **Application** emojis make that practical: 2000 per app, no
guild required and no ``USE_EXTERNAL_EMOJIS``, where the per-server cap of 250
would otherwise be too small for the ~270 ranks and force an emoji-server farm.

This uploads the whole set (idempotently — reruns only fill gaps) and writes
``static/rank_emojis.json``, the map ``utils/rank_emojis.py`` reads at runtime.

The icons come from the OSRS wiki, enumerated live via the MediaWiki API rather
than hardcoded: file casing is irregular (``Deputy_owner`` but ``Gnome_Child``,
``Record-chaser`` but ``Speed-Runner``), so a derived name would silently 404.
They are 13x13 pixel art, upscaled 8x nearest-neighbour before upload so
Discord's own downscale to inline text height stays crisp instead of smearing.

Usage:
    python scripts/seed_rank_emojis.py --dry-run     # list what would upload
    python scripts/seed_rank_emojis.py               # upload + write the map
    python scripts/seed_rank_emojis.py --verify      # diff app vs map vs wiki
    python scripts/seed_rank_emojis.py --prune       # delete rank_* the wiki dropped
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from interactions.api.http.http_client import HTTPClient  # noqa: E402
from interactions.client.utils.serializer import to_image_data  # noqa: E402

from utils.rank_emojis import MAP_PATH, emoji_name, normalize_rank  # noqa: E402
from utils.wiki_ua import USER_AGENT  # noqa: E402  (one identity for the wiki)

WIKI_API = "https://oldschool.runescape.wiki/api.php"
WIKI_FILE_PREFIX = "Clan icon - "
#: The wiki's CDN 403s the default urllib/curl agent; identify the bot properly.

#: 13x13 source art -> 104x104. Nearest-neighbour keeps the pixels square.
UPSCALE_FACTOR = 8

#: Discord's per-app ceiling. Well above the ~270 ranks, but refuse to start a
#: run that would hit it rather than failing halfway through.
APP_EMOJI_LIMIT = 2000


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def list_wiki_rank_icons() -> dict:
    """``{normalized rank: wiki file name}`` for every ``Clan icon - *.png``."""
    icons, cont = {}, {}
    while True:
        params = {
            "action": "query", "list": "allimages", "aiprefix": WIKI_FILE_PREFIX,
            "ailimit": "500", "format": "json", **cont,
        }
        data = json.loads(_get(f"{WIKI_API}?{urllib.parse.urlencode(params)}"))
        for item in data.get("query", {}).get("allimages", []):
            name = item["name"]
            if not name.lower().endswith(".png"):
                continue
            rank = name[len(WIKI_FILE_PREFIX.replace(" ", "_")):-len(".png")]
            key = normalize_rank(rank)
            if key:
                icons[key] = name
        cont = data.get("continue") or {}
        if not cont:
            return icons


def fetch_icon_png(file_name: str) -> bytes:
    """Download one wiki icon and upscale it for Discord."""
    raw = _get(f"https://oldschool.runescape.wiki/images/{urllib.parse.quote(file_name)}")
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    image = image.resize(
        (image.width * UPSCALE_FACTOR, image.height * UPSCALE_FACTOR), Image.NEAREST
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def write_map(mapping: dict) -> None:
    os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
    with open(MAP_PATH, "w", encoding="utf-8") as fh:
        json.dump(dict(sorted(mapping.items())), fh, indent=2, sort_keys=True)
        fh.write("\n")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dry-run", action="store_true", help="list work, upload nothing")
    ap.add_argument("--verify", action="store_true", help="report drift, change nothing")
    ap.add_argument("--prune", action="store_true", help="delete rank_* emojis the wiki no longer has")
    ap.add_argument("--limit", type=int, default=0, help="cap uploads this run (0 = no cap)")
    args = ap.parse_args()

    token = os.getenv("BOT_TOKEN")
    if not token:
        print("error: BOT_TOKEN not set", file=sys.stderr)
        return 2

    print(f"enumerating {WIKI_FILE_PREFIX}*.png from the wiki...")
    wiki = list_wiki_rank_icons()
    print(f"  {len(wiki)} rank icons")

    http = HTTPClient()
    await http.login(token)
    try:
        app_id = (await http.get_current_bot_information())["id"]
        existing = {e["name"]: e["id"] for e in await http.get_application_emojis(app_id)}
        print(f"app {app_id} currently owns {len(existing)} emoji(s)")

        wanted = {key: emoji_name(key) for key in wiki}
        missing = {k: n for k, n in wanted.items() if n not in existing}
        stale = [n for n in existing if n.startswith("rank_") and n not in set(wanted.values())]

        if args.verify:
            mapped = {}
            if os.path.exists(MAP_PATH):
                with open(MAP_PATH, encoding="utf-8") as fh:
                    mapped = json.load(fh)
            print(f"  uploaded : {len(wanted) - len(missing)}/{len(wanted)}")
            print(f"  missing  : {sorted(missing)[:10]}{' ...' if len(missing) > 10 else ''}")
            print(f"  stale    : {stale or 'none'}")
            print(f"  map file : {len(mapped)} entries at {MAP_PATH}")
            unmapped = [k for k in wanted if k not in mapped]
            print(f"  unmapped : {sorted(unmapped)[:10]}{' ...' if len(unmapped) > 10 else ''}")
            return 0

        if args.prune:
            for name in stale:
                await http.delete_application_emoji(app_id, existing[name])
                print(f"  deleted :{name}:")
                existing.pop(name, None)

        if len(existing) + len(missing) > APP_EMOJI_LIMIT:
            print(f"error: {len(existing)} + {len(missing)} exceeds the {APP_EMOJI_LIMIT} "
                  "app emoji limit", file=sys.stderr)
            return 1

        todo = sorted(missing.items())
        if args.limit:
            todo = todo[: args.limit]
        print(f"{len(missing)} to upload{f' (capped at {len(todo)})' if args.limit else ''}")

        if args.dry_run:
            for key, name in todo:
                print(f"  would upload :{name}: from {wiki[key]}")
        else:
            for index, (key, name) in enumerate(todo, 1):
                try:
                    png = fetch_icon_png(wiki[key])
                    created = await http.create_application_emoji(
                        {"name": name, "image": to_image_data(png)}, app_id
                    )
                    existing[name] = created["id"]
                    print(f"  [{index}/{len(todo)}] :{name}: -> {created['id']}")
                except Exception as e:
                    # One bad icon must not abandon the other 269; it shows up
                    # as missing on the next --verify.
                    print(f"  [{index}/{len(todo)}] FAILED {name} ({wiki[key]}): {e}",
                          file=sys.stderr)

        mapping = {key: f"<:{name}:{existing[name]}>"
                   for key, name in wanted.items() if name in existing}
        if not args.dry_run:
            write_map(mapping)
            print(f"wrote {len(mapping)} entries to {MAP_PATH}")
        return 0
    finally:
        await http.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
