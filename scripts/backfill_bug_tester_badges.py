#!/usr/bin/env python3
"""
Give the Bug Tester badge to everyone who holds the Discord role without it
===========================================================================

Before the role sync existed, the "Bug Tester" role on the main server was
handed out by hand and drifted from the badge. Once the badge drives the
role, anyone holding the role without the badge would lose it. This awards
the badge to those people instead, on their main account (highest total
level), the same way ``/bug-tester add`` does. The badge also grants the
complimentary supporter perks.

People with no DropTracker account, or no claimed in-game name, can't hold a
badge. They are listed and left alone here; the role sync then removes their
role until they link an account.

Uses the webhook bot's token (``WEBHOOK_TOKEN``) to list the role's holders,
since that needs the GUILD_MEMBERS intent.

Usage:
    python scripts/backfill_bug_tester_badges.py            # dry run
    python scripts/backfill_bug_tester_badges.py --apply    # award the badges
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

from services import discord_roles as dr  # noqa: E402

#: The bot owner the awards are attributed to, as the existing ones are.
DEFAULT_ACTOR_DISCORD_ID = "528746710042804247"


async def _role_holders() -> list:
    token = os.getenv("WEBHOOK_TOKEN")
    if not token:
        raise SystemExit("error: WEBHOOK_TOKEN not set")
    role_id = dr.load_role_map().get("bug_tester") or dr.SPECS_BY_KEY["bug_tester"].adopt_role_id
    http = HTTPClient()
    await http.login(token)
    try:
        members = await dr.fetch_guild_members(http)
    finally:
        await http.close()
    return [m for m in members
            if str(role_id) in {str(r) for r in m.get("roles") or ()}
            and not (m.get("user") or {}).get("bot")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true", help="award the badges (default: dry run)")
    parser.add_argument("--actor-discord-id", default=DEFAULT_ACTOR_DISCORD_ID,
                        help="Discord id of the owner the awards are attributed to")
    args = parser.parse_args()

    holders = asyncio.run(_role_holders())
    print(f"Bug Tester role holders: {len(holders)}")

    from db.models import Session, User

    verbs = {"granted": "awarded" if args.apply else "would award", "already": "has the badge",
             "no_user": "no DropTracker account", "no_players": "no claimed in-game name",
             "no_badge": "badge missing/inactive"}
    counts = {}
    with Session() as session:
        actor = session.query(User.user_id).filter(User.discord_id == str(args.actor_discord_id)).first()
        if actor is None:
            print(f"error: no DropTracker user for Discord id {args.actor_discord_id}", file=sys.stderr)
            return 2
        actor_user_id = int(actor[0])
        for member in holders:
            user = member.get("user") or {}
            change = dr.grant_bug_tester(session, str(user.get("id")), actor_user_id,
                                         dry_run=not args.apply)
            counts[change.status] = counts.get(change.status, 0) + 1
            name = member.get("nick") or user.get("global_name") or user.get("username")
            where = f" on {', '.join(change.player_names)}" if change.player_names else ""
            print(f"  {verbs.get(change.status, change.status):<24} {name} ({user.get('id')}){where}")
        if args.apply:
            session.commit()
    if args.apply and counts.get("granted"):
        from services.tester_roster import notify_changed

        notify_changed()  # the dev instance and the edge Worker pick the new testers up now
    print(f"summary: {counts}")
    if not args.apply:
        print("[DRY RUN] nothing changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
