"""Permanently erase a player (and optionally their user account) on request.

The rules for what is deleted, moved or kept live in ``db/player_erasure.py``.
In short: the player's own data is deleted, past/present event rows move to a
hidden "Removed player #N" placeholder so standings do not change, tickets are
kept but unlinked, and the bot deletes the Discord posts it made about them.

Order on --apply: Discord posts, then the DB (one transaction), then Redis,
then screenshot files. Every step is idempotent, so a failed run can be
re-run.

Dry-run by default.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.purge_player \\
        --player 5756157 --expect-name "Of Fury" --user 2833 [--apply]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import text  # noqa: E402

from db import player_erasure as pe  # noqa: E402
from db.models.base import Session  # noqa: E402


def delete_discord_messages(messages, apply: bool) -> dict:
    """DELETE each post with the core bot's token. 404 (already gone) and 403
    (bot lost access) are reported, not fatal."""
    out = {"deleted": 0, "gone": 0, "forbidden": 0, "failed": []}
    if not apply or not messages:
        return out
    import httpx

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN not set")
    headers = {"Authorization": f"Bot {token}",
               "X-Audit-Log-Reason": "Player data erasure request"}
    with httpx.Client(timeout=10.0, headers=headers) as client:
        for channel_id, message_id in messages:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
            for _attempt in range(5):
                r = client.delete(url)
                if r.status_code == 429:
                    time.sleep(float(r.json().get("retry_after", 1.0)) + 0.1)
                    continue
                break
            if r.status_code == 204:
                out["deleted"] += 1
            elif r.status_code == 404:
                out["gone"] += 1
            elif r.status_code == 403:
                out["forbidden"] += 1
                out["failed"].append((channel_id, message_id, 403))
            else:
                out["failed"].append((channel_id, message_id, r.status_code))
    return out


def _print_plan(plan: pe.Plan) -> None:
    print(f"player {plan.player_id} {plan.player_name!r} wom={plan.wom_id}  "
          f"user {plan.user_id} discord={plan.discord_id}")
    for title, d in (("move to placeholder player", plan.reassign),
                     ("delete (player rows)", plan.delete),
                     ("delete (user rows)", plan.delete_user),
                     ("set NULL (actor columns)", plan.null_user),
                     ("set NULL (event user columns)", plan.nullable_event_user),
                     ("move to 'Removed user'", plan.ticket_user)):
        if d:
            print(f"\n{title}:")
            for k, n in d.items():
                print(f"  {'.'.join(k[:2])}: {n}")
    if plan.children:
        print("\ndelete (FK children of the player's rows):")
        for (ct, cc, pt), n in plan.children.items():
            print(f"  {ct}.{cc} -> {pt}: {n}")
    print(f"\nwom_group_member_ids rows: {plan.wom_member_rows}")
    print(f"discord posts to delete: {len(plan.discord_messages)}")
    print(f"image urls on rows: {len(plan.image_urls)}")
    if plan.refusals:
        print("\nREFUSALS:")
        for r in plan.refusals:
            print(f"  {r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--player", type=int, help="player_id to erase")
    ap.add_argument("--expect-name", help="refuse unless players.player_name matches")
    ap.add_argument("--user", type=int, help="user_id to erase as well")
    ap.add_argument("--skip-discord", action="store_true", help="leave posted messages")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rehearse", action="store_true",
                    help="run the DB transaction, then roll it back (no Discord/Redis/files)")
    args = ap.parse_args()
    if args.player is None and args.user is None:
        ap.error("give --player and/or --user")
    if args.apply and args.player is not None and args.expect_name is None:
        ap.error("--apply with --player requires --expect-name")

    s = Session()
    try:
        if not (args.apply or args.rehearse):
            s.execute(text("SET SESSION TRANSACTION READ ONLY"))
        plan = pe.plan_erasure(s, args.player, args.user)
        if args.expect_name is not None and plan.player_name != args.expect_name:
            plan.refusals.append(f"name is {plan.player_name!r}, --expect-name said {args.expect_name!r}")
        _print_plan(plan)

        from utils.redis import redis_client
        print("\nredis:", pe.purge_redis(redis_client.client, args.player, args.user, apply=False))
        print("files:", pe.purge_files(plan, apply=False))

        if args.rehearse and not plan.refusals:
            done = pe.apply_erasure(s, plan)
            s.rollback()
            print("\nrehearsal (rolled back):")
            for k, v in done.items():
                print(f"  {k}: {v}")
            return 0
        if not args.apply:
            print("\n(dry run, nothing written)")
            return 0
        if plan.refusals:
            print("\nnot applying: refusals above")
            return 1

        if not args.skip_discord:
            print("\ndiscord:", delete_discord_messages(plan.discord_messages, apply=True))
        done = pe.apply_erasure(s, plan)
        s.commit()
        print("\ndb:")
        for k, v in done.items():
            print(f"  {k}: {v}")
        print("redis:", pe.purge_redis(redis_client.client, args.player, args.user, apply=True))
        print("files:", pe.purge_files(plan, apply=True))
        return 0
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
