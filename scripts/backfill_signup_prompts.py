"""Record sign-up prompts the bot posted *before* web70a, so they can be retired.

The retire sweep (services/event_signup_prompt.py) can only edit prompts it
knows about, and prompts posted before ``web_event_signup_messages`` existed
were never recorded — their "Sign up" button would sit there forever even
after the event began (the exact bug web70a fixes). This one-shot walks the
recent history of each event's announcements channel, finds the bot's own
prompt messages by their ``evtsignup:{event_id}`` button, and inserts the
missing rows. The sweep then retires them on its next pass.

Idempotent: a message already recorded is skipped, and a prompt whose event
has since closed is left for the sweep to edit (this script never touches
Discord messages, only the table).

Runs REST-only (``bot.login()``, no gateway) so it is safe to run while
droptracker-core is live.

    cd /store/droptracker/disc && venv/bin/python -m scripts.backfill_signup_prompts
    …                                                            --events 35 --limit 200
    …                                                            --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

DEFAULT_SCAN = 100  # messages of channel history per channel


def _prompt_button_ids(message) -> set:
    """Event ids of any ``evtsignup:{id}`` button on this message.

    Walks the component tree rather than two fixed levels: a Components-V2
    prompt nests its ActionRow inside a Container, so the button sits deeper
    than on a legacy embed post.
    """
    found = set()

    def walk(node, depth=0):
        if depth > 5:
            return
        custom_id = getattr(node, "custom_id", None) or ""
        if custom_id.startswith("evtsignup:"):
            tail = custom_id.split(":", 1)[1]
            if tail.isdigit():
                found.add(int(tail))
        for child in getattr(node, "components", None) or []:
            walk(child, depth + 1)

    walk(message)
    return found


async def backfill(bot, session, event_ids=None, limit=DEFAULT_SCAN,
                   dry_run=False) -> dict:
    from db.models import Event, EventChannel, EventSignupMessage

    query = (
        session.query(EventChannel, Event)
        .join(Event, Event.id == EventChannel.event_id)
        .filter(EventChannel.kind == "announcements")
    )
    if event_ids:
        query = query.filter(Event.id.in_(event_ids))
    # A past event's prompt is already irrelevant — nobody is reading it.
    rows = [(c, e) for c, e in query.all() if e.status != "past" or event_ids]

    known = {
        (r.event_id, r.message_id)
        for r in session.query(EventSignupMessage).all()
    }
    found, added, channels = 0, 0, 0
    for channel_row, event in rows:
        try:
            channel = await bot.fetch_channel(channel_id=channel_row.channel_id)
        except Exception as e:  # noqa: BLE001
            print(f"[backfill] event {event.id}: channel {channel_row.channel_id} "
                  f"unreachable ({type(e).__name__}: {e})")
            continue
        if channel is None or not hasattr(channel, "history"):
            continue
        channels += 1
        try:
            messages = await channel.history(limit=limit).flatten()
        except Exception as e:  # noqa: BLE001
            print(f"[backfill] event {event.id}: history failed "
                  f"({type(e).__name__}: {e})")
            continue
        for message in messages:
            if event.id not in _prompt_button_ids(message):
                continue
            found += 1
            key = (event.id, str(message.id))
            if key in known:
                continue
            known.add(key)
            added += 1
            print(f"[backfill] event {event.id} ({event.name}): recording "
                  f"prompt {message.id} in #{channel_row.channel_id}")
            if not dry_run:
                session.add(EventSignupMessage(
                    event_id=event.id,
                    channel_id=str(channel_row.channel_id),
                    message_id=str(message.id),
                    group_id=getattr(channel_row, "group_id", None),
                ))
    if not dry_run:
        session.commit()
    return {"channels": channels, "found": found, "added": added}


async def _run(token: str, event_ids, limit: int, dry_run: bool) -> dict:
    import interactions

    from db.models import Session

    bot = interactions.Client(token=token)
    # REST-only: login() authenticates bot.http without starting a gateway, so
    # this cannot clash with the live droptracker-core session on this token.
    await bot.login(token)
    session = Session()
    try:
        return await backfill(bot, session, event_ids, limit, dry_run)
    finally:
        session.close()
        # http.close(), not bot.stop() — stop() dereferences a gateway that was
        # never started and raises.
        try:
            await bot.http.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", help="comma-separated event ids (default: all "
                                         "non-past events with an announcements channel)")
    parser.add_argument("--limit", type=int, default=DEFAULT_SCAN,
                        help=f"messages of history to scan per channel (default {DEFAULT_SCAN})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be recorded, write nothing")
    args = parser.parse_args()

    load_dotenv()
    token = os.getenv("BOT_TOKEN") if os.getenv("STATE") != "dev" else os.getenv("DEV_TOKEN")
    if not token:
        print("[backfill] no bot token in the environment (BOT_TOKEN/DEV_TOKEN)")
        return 1
    event_ids = None
    if args.events:
        event_ids = [int(x) for x in args.events.split(",") if x.strip().isdigit()]

    result = asyncio.run(_run(token, event_ids, args.limit, args.dry_run))
    print(f"[backfill] scanned {result['channels']} channel(s), "
          f"found {result['found']} prompt message(s), "
          f"recorded {result['added']}"
          f"{' (dry run — nothing written)' if args.dry_run else ''}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
