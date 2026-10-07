#!/usr/bin/env python3
"""
Create the HQ #updates channel and retire the three old update channels
======================================================================

#news stays the public place for important changes. #updates takes the
smaller ones (fixes, tweaks, new options) and replaces the separate plugin /
website / discord update channels. It is an Announcement channel, so leaders
can Follow it into their own server, and it is role-gated: only the
"Follows Updates" role (clan leaders by default, see services/news_optin.py)
can see it.

Permission overwrites start from #news's (so staff roles keep what they have
there), then: @everyone can't see it, Follows Updates can read it, and the
core bot can post and publish.

Uses the core bot's token (BOT_TOKEN). Dry run unless ``--apply``; deleting
the old channels is a separate, explicit ``--delete-old``.

Usage:
    python scripts/setup_updates_channel.py                         # dry run
    python scripts/setup_updates_channel.py --apply                 # create #updates
    python scripts/setup_updates_channel.py --apply --delete-old    # + delete the old three
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from interactions.api.http.http_client import HTTPClient  # noqa: E402

from services import leader_updates as lu  # noqa: E402

CHANNEL_NAME = "📝┃updates"
TOPIC = ("Smaller DropTracker changes: fixes, tweaks and new options. "
         "Bigger news goes in #news. Follow this channel to get it in your own server.")
FOLLOW_ROLE_NAME = "Follows Updates"
GUILD_NEWS = 5

#: The channels #updates replaces (plugin forum, website, discord).
OLD_CHANNEL_IDS = (1528029600104583208, 1528029728483704873, 1528029693079588924)

VIEW = 1 << 10
SEND = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED = 1 << 14
ATTACH = 1 << 15
HISTORY = 1 << 16


def _overwrites(news: dict, guild_id: str, role_id: str, bot_id: str) -> list[dict]:
    out = {
        str(o["id"]): {"id": str(o["id"]), "type": int(o["type"]),
                       "allow": int(o.get("allow") or 0), "deny": int(o.get("deny") or 0)}
        for o in news.get("permission_overwrites") or []
    }
    everyone = out.setdefault(guild_id, {"id": guild_id, "type": 0, "allow": 0, "deny": 0})
    everyone["allow"] &= ~VIEW
    everyone["deny"] |= VIEW | SEND
    role = out.setdefault(role_id, {"id": role_id, "type": 0, "allow": 0, "deny": 0})
    role["allow"] |= VIEW | HISTORY
    role["deny"] = (role["deny"] | SEND) & ~(VIEW | HISTORY)
    bot = out.setdefault(bot_id, {"id": bot_id, "type": 1, "allow": 0, "deny": 0})
    bot["allow"] |= VIEW | SEND | EMBED | ATTACH | HISTORY | MANAGE_MESSAGES
    bot["deny"] = 0
    return [{**o, "allow": str(o["allow"]), "deny": str(o["deny"])} for o in out.values()]


async def _run(apply: bool, delete_old: bool) -> int:
    token = os.getenv("BOT_TOKEN")
    if not token:
        print("error: BOT_TOKEN not set")
        return 2
    guild_id = str(lu.HQ_GUILD_ID)
    http = HTTPClient()
    me = await http.login(token)
    try:
        channels = await http.get_guild_channels(guild_id)
        by_id = {str(c["id"]): c for c in channels}
        news = by_id.get(str(lu.NEWS_CHANNEL_ID))
        if news is None:
            print(f"error: #news ({lu.NEWS_CHANNEL_ID}) not found in HQ")
            return 2
        roles = await http.get_roles(guild_id)
        role = next((r for r in roles if r["name"] == FOLLOW_ROLE_NAME), None)
        if role is None:
            print(f"error: role '{FOLLOW_ROLE_NAME}' not found; restart droptracker-core to create it")
            return 2

        existing = next((c for c in channels if c.get("name") == CHANNEL_NAME
                         and int(c.get("type", -1)) == GUILD_NEWS), None)
        print(f"#news: {news['name']} ({news['id']}), category {news.get('parent_id')}, "
              f"position {news.get('position')}")
        print(f"role: {role['name']} ({role['id']})")
        if existing:
            print(f"#updates already exists: {existing['id']} (nothing to create)")
            updates_id = str(existing["id"])
        else:
            overwrites = _overwrites(news, guild_id, str(role["id"]), str(me["id"]))
            print(f"would create #{CHANNEL_NAME} (Announcement channel) under the same category, "
                  f"just below #news, with {len(overwrites)} permission overwrites")
            updates_id = None
            if apply:
                created = await http.create_guild_channel(
                    guild_id, CHANNEL_NAME, GUILD_NEWS, topic=TOPIC,
                    position=(news.get("position") or 0) + 1,
                    permission_overwrites=overwrites, parent_id=news.get("parent_id"),
                    reason="Smaller updates channel (replaces plugin/website/discord updates)",
                )
                updates_id = str(created["id"])
                print(f"CREATED #{CHANNEL_NAME}: {updates_id}")

        print("old update channels:")
        for cid in OLD_CHANNEL_IDS:
            c = by_id.get(str(cid))
            print(f"  {cid}: {c['name'] if c else '(already gone)'}")
            if c and apply and delete_old:
                await http.delete_channel(cid, reason="Replaced by #updates")
                print("    DELETED")
        if not apply:
            print("[DRY RUN] nothing changed. Re-run with --apply (and --delete-old to remove the old three).")
        elif updates_id:
            print(f"Set UPDATES_CHANNEL_ID = {updates_id} in services/leader_updates.py if it isn't already.")
    finally:
        await http.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-old", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args.apply, args.delete_old))


if __name__ == "__main__":
    sys.exit(main())
