"""One-off: retroactively award Nitro-boost credit to everyone CURRENTLY
boosting the main DropTracker Discord, and QUEUE the confirmation DMs (with a
clan picker for multi-group boosters) + ONE consolidated contributors-channel
thank-you.

The messages are SENT by the MAIN bot (droptracker-core / notification_service)
via the notification queue — it shares more guilds and is more recognizable than
the internal webhook bot. This script only lists boosters, awards credit, and
enqueues; the main bot's notification_service must be running to drain the queue.

Dry-run by default (reads only). Use --apply to write legs + queue notifications.

    python -m scripts.backfill_nitro_boosts                 # dry run (safe)
    python -m scripts.backfill_nitro_boosts --apply         # award + queue DMs + summary
    python -m scripts.backfill_nitro_boosts --apply --no-dm       # legs + summary only
    python -m scripts.backfill_nitro_boosts --apply --no-announce # legs + DMs only
    python -m scripts.backfill_nitro_boosts --limit 3

NOTE: re-running --apply queues fresh DMs, so the main bot would DM boosters
again — only re-run if you intend to re-notify.
"""
from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

import interactions  # noqa: E402

from db.models import Session  # noqa: E402
from services import nitro_attribution as na  # noqa: E402
from services import nitro_notifications as nn  # noqa: E402


def _token() -> str | None:
    return os.getenv("DEV_WEBHOOK_TOKEN") if os.getenv("STATUS") == "dev" else os.getenv("WEBHOOK_TOKEN")


async def main(args: argparse.Namespace) -> None:
    token = _token()
    if not token:
        raise SystemExit("No webhook bot token in env (WEBHOOK_TOKEN / DEV_WEBHOOK_TOKEN).")

    # REST-only login (no gateway) — just to list guild members.
    bot = interactions.Client(token=token)
    await bot.login(token)
    try:
        boosters = sorted(await na.fetch_booster_discord_ids(bot.http))
        if args.limit:
            boosters = boosters[: args.limit]
        print(f"Current boosters on the main guild: {len(boosters)}")
        if not boosters:
            print("Nothing to do.")
            return

        # Boost SLOTS, not headcount — a member can boost more than once. The
        # guild total is the ceiling; the boost system messages attribute what
        # they can (see services/nitro_attribution.py).
        state = await na.fetch_guild_boost_state(bot.http)
        observed = (
            await na.fetch_boost_message_counts(bot.http, state["system_channel_id"])
            if state.get("boost_messages_enabled")
            else {}
        )

        with Session() as s:
            contexts = {b: na.booster_context(s, b) for b in boosters}
            merged = dict(na.load_observed_counts(s, boosters))
            merged.update(observed)
            slots, diagnostics = na.resolve_boost_counts(
                boosters, merged, na.load_boost_overrides(s, boosters), state.get("total")
            )
            counts = na.attribute_boosters(s, slots)
        credited = sum(counts.values())
        credited_cents = credited * na.NITRO_BOOST_CENTS
        print(
            f"Guild reports {diagnostics['guild_total']} boost slot(s); "
            f"attributed {diagnostics['attributed']} across {diagnostics['boosters']} booster(s)."
        )
        if diagnostics["unattributed"]:
            print(
                f"  {diagnostics['unattributed']} slot(s) could not be traced to a member — "
                f"left uncredited; assign them on /admin/nitro-boosts."
            )
        entries = [
            (b, contexts[b].get("picked_group_name") if contexts[b].get("linked") else None)
            for b in boosters
        ]
        linked = sum(1 for b in boosters if contexts[b].get("linked"))
        print(
            f"Linked: {linked}/{len(boosters)}  |  credited to a group: {credited}  "
            f"({na.format_cents(credited_cents)}/mo total)"
        )
        print(f"Per-group credit: { {gid: c for gid, c in counts.items()} }")

        if not args.apply:
            print("\n--- DRY RUN (no legs written, no notifications queued) ---")
            print(f"Would award/refresh {len(counts)} group nitro leg(s).")
            print(f"Would queue {0 if args.no_dm else len(boosters)} booster DM notification(s).")
            print(f"Would queue {0 if args.no_announce else 1} consolidated summary notification.")
            print("(The main bot's notification_service sends them.)")
            for b in boosters[:8]:
                c = contexts[b]
                print(
                    f"  {b}: linked={c['linked']} groups={[g['name'] for g in c['groups']]} "
                    f"-> {c.get('picked_group_name')}"
                )
            return

        # ---- APPLY ----
        with Session() as s:
            stats = na.run_reconcile(
                s, set(boosters), observed_counts=observed, guild_total=state.get("total")
            )
        print(f"\nAwarded credit legs: {stats}")

        if not args.no_dm:
            queued = sum(1 for b in boosters if nn.queue_nitro_boost(b, announce=False))
            print(f"Queued {queued}/{len(boosters)} booster DM notification(s) (main bot will send).")
        if not args.no_announce:
            if nn.queue_nitro_boost_summary(entries, credited_cents):
                print("Queued 1 consolidated contributors-channel announcement (main bot will post).")
            else:
                print("Consolidated announcement not queued (duplicate or error).")
    finally:
        for closer in ("close", "stop"):
            fn = getattr(bot, closer, None) or getattr(getattr(bot, "http", None), closer, None)
            if fn:
                try:
                    res = fn()
                    if asyncio.iscoroutine(res):
                        await res
                    break
                except Exception:
                    pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Retroactively award + queue Nitro boost notifications.")
    p.add_argument("--apply", action="store_true", help="Write legs and queue notifications.")
    p.add_argument("--no-dm", action="store_true", help="Skip the per-booster DM notifications.")
    p.add_argument("--no-announce", action="store_true", help="Skip the consolidated channel summary.")
    p.add_argument("--limit", type=int, default=0, help="Only process the first N boosters (testing).")
    asyncio.run(main(p.parse_args()))
