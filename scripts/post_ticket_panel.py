"""Publish the Discord "open a ticket" panel, from the type registry.

The live panel was posted once and its buttons existed nowhere in this repo,
so adding a ticket type updated the website and the welcome card but could
never grow a button. This renders it from
``services.ticket_system.TICKET_TYPE_META`` — the same registry those two read
— so the three cannot drift again.

**It edits in place by default.** A panel is usually pinned or linked to, and
re-posting would move it, break those links and leave a dead message with live
buttons above it.

**It uses WEBHOOK_TOKEN, not BOT_TOKEN.** A bot may only edit its own
messages, and the live panel belongs to the webhook-manager application —
which is correct, because ``bots/webhook_bot.py`` is what loads the ticket
extension, so that is also the app Discord delivers the button clicks to. The
core bot cannot touch it.

The edit sends **only** the components. Discord leaves unlisted fields alone,
so the panel keeps its embed and wording; this script has no business
rewriting copy somebody chose.

Not routed through the Discord outbox: that queue is link-buttons-only by
design (``services/discord_outbox._build_components``) precisely so it never
needs custom_id routing. This talks to the REST API directly, like
``scripts/remove_bogus_drop.py``, and needs no gateway connection.

    # find the panel and show what would change
    ./venv/bin/python -m scripts.post_ticket_panel
    # edit it in place
    ./venv/bin/python -m scripts.post_ticket_panel --apply
    # first time only: create one
    ./venv/bin/python -m scripts.post_ticket_panel --channel <id> --create --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv

load_dotenv()

#: Only used when creating a panel from scratch. An existing one keeps its own.
PANEL_CONTENT = (
    "## Need a hand?\n"
    "Pick whichever fits best and we'll open a private ticket with you."
)

#: Channel names worth scanning when no --channel is given. Cheap heuristic:
#: the alternative is reading history for every channel in the guild.
_LIKELY_NAMES = ("ticket", "support", "help", "contact")


def panel_buttons() -> list:
    """Action rows built from the registry — the only thing an edit changes."""
    from services.ticket_system import TICKET_TYPE_META

    buttons = [
        {
            "type": 2,
            # The catch-all is visually secondary so it reads as the fallback.
            "style": 2 if key == "other" else 1,
            "label": meta["label"],
            "emoji": {"name": meta["emoji"]},
            "custom_id": f"create_ticket_{key}",
        }
        for key, meta in TICKET_TYPE_META.items()
    ]
    return [{"type": 1, "components": buttons[i:i + 5]}
            for i in range(0, len(buttons), 5)]


def _is_panel(message: dict) -> bool:
    """Whether a message is our ticket panel — it carries our button ids."""
    for row in message.get("components") or []:
        for component in row.get("components") or []:
            if str(component.get("custom_id", "")).startswith("create_ticket_"):
                return True
    return False


async def _find_panel(rest, channel_ids):
    """``(channel_id, message, buttons)`` for the first panel found."""
    for channel_id in channel_ids:
        try:
            messages = await rest._request(
                "GET", f"/channels/{channel_id}/messages?limit=50")
        except Exception:
            continue
        for message in messages or []:
            if _is_panel(message):
                ids = [c.get("custom_id") for row in message.get("components", [])
                       for c in row.get("components", [])]
                return channel_id, message, ids
    return None, None, None


async def _candidate_channels(rest, guild_id: str):
    """Text channels whose name suggests support, most likely first."""
    channels = await rest._request("GET", f"/guilds/{guild_id}/channels")
    text = [c for c in channels or [] if c.get("type") in (0, 5)]
    likely = [c for c in text if any(n in (c.get("name") or "").lower()
                                     for n in _LIKELY_NAMES)]
    return [str(c["id"]) for c in likely], [(c["id"], c.get("name")) for c in likely]


async def run(args) -> int:
    from utils.discord_rest import DiscordRest

    # The webhook-manager app owns the panel and handles its clicks.
    token = os.getenv("WEBHOOK_TOKEN")
    if not token:
        print("WEBHOOK_TOKEN is not set — the panel belongs to the webhook bot, "
              "and only its author can edit it.")
        return 1

    rows = panel_buttons()
    print("Panel would offer these buttons, in this order:")
    from services.ticket_system import TICKET_TYPE_META
    for key, meta in TICKET_TYPE_META.items():
        print(f"  {meta['emoji']}  {meta['label']:<24} create_ticket_{key}")
    print()

    async with DiscordRest(token, user_agent="DropTracker-ticket-panel/1.0") as rest:
        if args.channel:
            channel_ids = [str(args.channel)]
            print(f"Looking in channel {args.channel}…")
        else:
            guild_id = os.getenv("PRIMARY_GUILD_ID")
            if not guild_id:
                print("No --channel and PRIMARY_GUILD_ID is unset.")
                return 1
            channel_ids, named = await _candidate_channels(rest, guild_id)
            print(f"Scanning {len(channel_ids)} likely channel(s): "
                  + ", ".join(f"#{n}" for _i, n in named))

        found_channel, message, existing = await _find_panel(rest, channel_ids)

        if message is not None:
            print(f"\nFound the existing panel: message {message['id']} in {found_channel}")
            print(f"  it currently has {len(existing)} button(s): {', '.join(existing)}")
            missing = [b["custom_id"] for row in rows
                       for b in row["components"] if b["custom_id"] not in existing]
            print(f"  missing: {', '.join(missing) if missing else 'nothing — already current'}")
            if not args.apply:
                print("\nDry run — re-run with --apply to EDIT that message in place.")
                return 0
            # Components only: content and embeds are left exactly as they are.
            await rest.edit_message(found_channel, str(message["id"]),
                                    {"components": rows})
            print("\nEdited in place. Position, pins, embed and wording all unchanged.")
            return 0

        print("\nNo existing panel found in the channel(s) searched.")
        if not args.create:
            print("Pass --channel <id> --create to post a new one "
                  "(kept separate so a failed search never silently duplicates it).")
            return 1
        if not args.channel:
            print("--create needs an explicit --channel.")
            return 1
        if not args.apply:
            print("Dry run — re-run with --apply to post it.")
            return 0
        await rest.post_message(str(args.channel),
                                {"content": PANEL_CONTENT, "components": rows})
        print("Posted.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="channel id (default: scan the guild)")
    parser.add_argument("--create", action="store_true",
                        help="post a new panel when none exists")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
