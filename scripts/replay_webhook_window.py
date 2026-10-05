"""Replay webhook-path submissions from Discord message history.

Webhook-only clients (``useApi=false``) POST straight to Discord webhook URLs,
so their submissions never touch our API. The reader bot
(``bots/webhook_bot.py``) turns each of those Discord messages into submissions
live in ``on_message_create`` — with **no queue and no retry**. If dispatch
raises (Redis wedged into MISCONF by a full disk, DB down, deploy blip), the
message is simply never processed and nothing anywhere records that it existed.

But the Discord message itself is still sitting in the channel. This replays a
window of that history back through the exact intake path.

Safe to run repeatedly: ``data/submissions/common.ensure_can_create`` dedups by
GUID with **no time bound**, so anything already recorded is a no-op. That is
the property this whole tool leans on — see the replay-idempotency notes before
changing it.

Dry run by default: prints what it *would* dispatch, writes nothing. Pass
``--apply`` to actually dispatch.

Run:
    venv/bin/python -m scripts.replay_webhook_window --start "2026-08-18 18:34" --end "2026-08-18 20:01"
    venv/bin/python -m scripts.replay_webhook_window --start ... --end ... --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

import interactions
from interactions import Intents

# Discord snowflakes encode a millisecond timestamp, so a time window converts
# straight into id bounds. Shared with the reader's automatic startup catch-up.
from services.webhook_catchup import collect_channels, snowflake_for, stamp_bundle
from utils.replay_stamp import TRUSTED_RECEIVED_AT_MAX_LAG

def parse_utc(value: str) -> datetime:
    """Accept 'YYYY-MM-DD HH:MM[:SS]' or ISO 8601; always interpreted as UTC."""
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"unrecognised UTC timestamp {value!r} (try '2026-08-18 18:34')"
    )


async def replay(args) -> int:
    # Imported here, not at module scope: importing the reader pulls in the DB
    # models and the whole submission stack, which is pointless work when the
    # user only asked for --help.
    from bots.webhook_bot import (
        build_message_bundle,
        process_message_bundle,
        target_guilds,
    )
    from data.submissions.common import suppress_notifications

    token = os.getenv("WEBHOOK_TOKEN")
    if not token:
        print("WEBHOOK_TOKEN is not set; cannot read the webhook channels")
        return 2

    guild_ids = args.guild or [str(g) for g in target_guilds]
    only_channels = set(args.channel or [])

    after_id = snowflake_for(args.start)
    before_id = snowflake_for(args.end)

    print(f"window : {args.start.isoformat()} -> {args.end.isoformat()} (UTC)")
    print(f"guilds : {', '.join(guild_ids)}")
    print(f"mode   : {'APPLY (dispatching)' if args.apply else 'dry run (nothing will be written)'}")
    print(f"muted  : {', '.join(args.suppress_notify) if args.suppress_notify else '(nothing — every notification fires)'}")

    # Trusted replay stamps are believed for TRUSTED_RECEIVED_AT_MAX_LAG (7 days);
    # past that common.received_at falls back to now().
    age = datetime.now(timezone.utc) - args.start
    if age > TRUSTED_RECEIVED_AT_MAX_LAG:
        print(f"WARNING: window starts {age.total_seconds() / 86400:.1f} days ago, past the "
              f"{TRUSTED_RECEIVED_AT_MAX_LAG.days}-day trusted-stamp limit — older replayed "
              f"rows will be stamped at the current time, not their original one.")
    print()

    # HTTP-only login. Deliberately not astart(): the reader bot is already on
    # the gateway with this same token, and a replay has no reason to open a
    # second session or receive live events while it works.
    client = interactions.Client(token=token, intents=Intents.DEFAULT)
    await client.login(token)

    try:
        # Held across the entire scan, not per message: the ContextVar has to be
        # set on the task that ultimately calls create_notification.
        with suppress_notifications(*args.suppress_notify):
            return await _scan(args, client, guild_ids, only_channels,
                               after_id, before_id,
                               build_message_bundle, process_message_bundle)
    finally:
        await client.stop()


async def _scan(args, client, guild_ids, only_channels, after_id, before_id,
                build_message_bundle, process_message_bundle) -> int:
    channels = await collect_channels(client, guild_ids, only_channels)
    print(f"scanning {len(channels)} readable text channel(s)\n")

    by_type = Counter()
    per_channel = Counter()
    messages_seen = 0
    messages_with_bundle = 0
    dispatched = 0
    failed = 0

    for guild, channel in channels:
        try:
            # interactions.py ignores `before` once `after` is set and pages
            # forward to the present, oldest first, so --end is enforced here.
            # (Before this check a replay ran from --start all the way to now.)
            history = channel.history(limit=args.limit, after=after_id)
            async for message in history:
                if int(message.id) >= before_id:
                    break
                # Mirror the live listener's own filters so a replay cannot
                # ingest something intake would have ignored.
                if message.author is None or message.author.system:
                    continue
                if client.user and message.author.id == client.user.id:
                    continue
                if not message.embeds:
                    continue

                messages_seen += 1
                bundle = build_message_bundle(message)
                if not bundle:
                    continue

                # Stamp each payload with when Discord actually received it, so
                # a replay books the drop at its real time instead of "whenever
                # the backfill ran". Set here and never in the shared bundler:
                # on the live path the two are the same instant anyway. The
                # stamp is marked trusted (it comes from Discord, not the
                # client), so common.received_at believes it for up to 7 days.
                stamp_bundle(bundle, message.created_at)
                messages_with_bundle += 1
                for submission_type, _ in bundle:
                    by_type[str(submission_type)] += 1
                per_channel[f"{guild.name}#{channel.name}"] += len(bundle)

                if args.verbose:
                    kinds = ", ".join(str(t) for t, _ in bundle)
                    print(f"  {message.created_at.isoformat()} #{channel.name}: {kinds}")

                if args.apply:
                    try:
                        dispatched += await process_message_bundle(message, bundle)
                    except Exception as e:
                        failed += 1
                        print(f"  ! dispatch failed for message {message.id}: {e}")
        except Exception as e:
            print(f"  ! {guild.name}#{channel.name}: history unavailable ({e})")
            continue

    print()
    print("channels with traffic in window:")
    for name, count in per_channel.most_common():
        print(f"  {name:<45} {count:>7,} embeds")
    print()
    print("by submission type:")
    for name, count in by_type.most_common():
        print(f"  {name:<25} {count:>7,}")
    print()
    print(f"messages in window : {messages_seen:,}")
    print(f"  with submissions : {messages_with_bundle:,}")
    print(f"embeds recoverable : {sum(by_type.values()):,}")
    if args.apply:
        print(f"embeds dispatched  : {dispatched:,}")
        print(f"messages failed    : {failed:,}")
    else:
        print("\nDry run — nothing was dispatched. Re-run with --apply to replay.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, type=parse_utc,
                        help="window start, UTC (e.g. '2026-08-18 18:34')")
    parser.add_argument("--end", required=True, type=parse_utc,
                        help="window end, UTC (e.g. '2026-08-18 20:01')")
    parser.add_argument("--apply", action="store_true",
                        help="actually dispatch (default: dry run)")
    parser.add_argument("--guild", action="append",
                        help="limit to this guild id (repeatable; default: the reader's target guilds)")
    parser.add_argument("--channel", action="append",
                        help="limit to this channel id (repeatable)")
    parser.add_argument("--limit", type=int, default=0,
                        help="max messages per channel (0 = no limit)")
    parser.add_argument("--suppress-notify", action="append", default=None,
                        metavar="TYPE",
                        help="mute this notification type during the replay (repeatable). "
                             "Use 'pb' and 'dm_pb' to replay personal bests without "
                             "announcing them hours late. The submission is still "
                             "recorded, scored and counted toward events.")
    parser.add_argument("--verbose", action="store_true",
                        help="print every message that carries submissions")
    args = parser.parse_args()
    args.suppress_notify = args.suppress_notify or []

    if args.end <= args.start:
        parser.error("--end must be after --start")

    return asyncio.run(replay(args))


if __name__ == "__main__":
    raise SystemExit(main())
