"""One-off: retroactively award Nitro-boost credit to everyone CURRENTLY
boosting the main DropTracker Discord, DM each of them a confirmation (with the
clan picker for multi-group boosters), and post ONE consolidated thank-you to
the contributors channel (not one message per booster).

Dry-run by default (reads only — no DB writes, no messages). Use --apply to
write the credit legs and send the messages.

IMPORTANT: run this AFTER droptracker-webhooks has been restarted with the nitro
code, so the DM clan-picker (`nitro_pick`) is handled. The credit itself is
awarded regardless of bot state.

Uses REST only (Client.login, no gateway), so it does not open a second gateway
session or interfere with the running webhook bot.

    python -m scripts.backfill_nitro_boosts                 # dry run (safe)
    python -m scripts.backfill_nitro_boosts --apply         # award + DM + announce
    python -m scripts.backfill_nitro_boosts --apply --no-dm # award + announce only
    python -m scripts.backfill_nitro_boosts --apply --no-announce
    python -m scripts.backfill_nitro_boosts --limit 3       # sample a few
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

CONTRIB_CHANNEL = int(os.getenv("DISCORD_CONTRIBUTION_CHANNEL_ID", "1490419196012793866"))
_EMBED_COLOR = 0x9B59B6  # amethyst


def _token() -> str | None:
    return os.getenv("DEV_WEBHOOK_TOKEN") if os.getenv("STATUS") == "dev" else os.getenv("WEBHOOK_TOKEN")


def _pick_components(context: dict) -> list:
    """DM clan-picker select (raw payload) — only when in >1 group."""
    groups = context.get("groups") or []
    if len(groups) < 2:
        return []
    picked = context.get("picked_group_id")
    return [
        {
            "type": 1,  # action row
            "components": [
                {
                    "type": 3,  # string select
                    "custom_id": "nitro_pick",
                    "placeholder": "Choose which clan your boost supports",
                    "min_values": 1,
                    "max_values": 1,
                    "options": [
                        {"label": str(g["name"])[:100], "value": str(g["id"]), "default": g["id"] == picked}
                        for g in groups[:25]
                    ],
                }
            ],
        }
    ]


def _announce_message(entries: list[tuple[str, str | None]], credited_cents: int) -> dict:
    """ONE message with the whole booster list as embed(s) (mentions in embeds
    don't ping). Chunks into <=10 embeds if the list is long."""
    lines = [f"• <@{did}> → **{grp}**" if grp else f"• <@{did}>" for did, grp in entries]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for ln in lines:
        if size + len(ln) + 1 > 3800 and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(ln)
        size += len(ln) + 1
    if buf:
        chunks.append("\n".join(buf))

    intro = (
        f"These members boost the **DropTracker** Discord — together contributing "
        f"**{na.format_cents(credited_cents)}/mo** of premium credit to their clans! Thank you 💜\n\n"
    )
    embeds = []
    for i, desc in enumerate(chunks[:10]):
        embed = {"color": _EMBED_COLOR, "description": (intro + desc) if i == 0 else desc}
        if i == 0:
            embed["title"] = "🚀 Thank you to our Server Boosters!"
        embeds.append(embed)
    if len(chunks) > 10:
        dropped = sum(c.count("\n") + 1 for c in chunks[10:])
        embeds[-1]["description"] += f"\n…and {dropped} more."
    return {"embeds": embeds, "allowed_mentions": {"parse": []}}


async def main(args: argparse.Namespace) -> None:
    token = _token()
    if not token:
        raise SystemExit("No webhook bot token in env (WEBHOOK_TOKEN / DEV_WEBHOOK_TOKEN).")
    if not na.process_nitro_boosts_enabled():
        print("NOTE: PROCESS_NITRO_BOOSTS is not enabled — the live bot reconciler/DMs are off, "
              "but this backfill will still run.")

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

        with Session() as s:
            contexts = {b: na.booster_context(s, b) for b in boosters}
            counts = na.attribute_boosters(s, set(boosters))
        credited = sum(counts.values())
        credited_cents = credited * na.NITRO_BOOST_CENTS
        entries = [
            (b, contexts[b].get("picked_group_name") if contexts[b].get("linked") else None)
            for b in boosters
        ]
        linked = sum(1 for b in boosters if contexts[b].get("linked"))
        print(
            f"Linked to a DropTracker account: {linked}/{len(boosters)}  |  "
            f"credited to a group: {credited}  ({na.format_cents(credited_cents)}/mo total)"
        )
        print(f"Per-group credit: { {gid: c for gid, c in counts.items()} }")

        if not args.apply:
            print("\n--- DRY RUN (no legs written, no messages sent) ---")
            print(f"Would award/refresh {len(counts)} group nitro leg(s).")
            print(f"Would DM {0 if args.no_dm else len(boosters)} booster(s).")
            print(f"Would post 1 consolidated message to channel {CONTRIB_CHANNEL}"
                  f"{' (skipped: --no-announce)' if args.no_announce else ''}.")
            print("\nSample:")
            for b in boosters[:8]:
                c = contexts[b]
                print(f"  {b}: linked={c['linked']} groups={[g['name'] for g in c['groups']]} "
                      f"-> {c.get('picked_group_name')}")
            preview = _announce_message(entries, credited_cents)
            print("\nAnnounce preview (embed 1):\n" + preview["embeds"][0]["description"][:1000])
            return

        # ---- APPLY ----
        with Session() as s:
            stats = na.run_reconcile(s, set(boosters))
        print(f"\nAwarded credit legs: {stats}")

        if not args.no_dm:
            sent = failed = 0
            for b in boosters:
                try:
                    dm = await bot.http.create_dm(int(b))
                    await bot.http.create_message(
                        {"content": na.nitro_boost_dm_text(contexts[b]), "components": _pick_components(contexts[b])},
                        dm["id"],
                    )
                    sent += 1
                except Exception as e:  # DMs closed / blocked / etc. — skip, keep going
                    failed += 1
                    print(f"  DM to {b} failed: {e}")
                await asyncio.sleep(args.dm_delay)
            print(f"DMs sent: {sent}, failed/closed: {failed}")

        if not args.no_announce:
            await bot.http.create_message(_announce_message(entries, credited_cents), CONTRIB_CHANNEL)
            print(f"Posted 1 consolidated announcement to channel {CONTRIB_CHANNEL}.")
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
    p = argparse.ArgumentParser(description="Retroactively award + announce Nitro boosts.")
    p.add_argument("--apply", action="store_true", help="Actually write legs and send messages.")
    p.add_argument("--no-dm", action="store_true", help="Skip the per-booster DMs.")
    p.add_argument("--no-announce", action="store_true", help="Skip the contributors-channel message.")
    p.add_argument("--limit", type=int, default=0, help="Only process the first N boosters (testing).")
    p.add_argument("--dm-delay", type=float, default=0.4, help="Seconds between DMs (rate-limit friendly).")
    asyncio.run(main(p.parse_args()))
