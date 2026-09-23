#!/usr/bin/env python3
"""
Run one pass of the tier, Bug Tester and Registered role sync by hand
=====================================================================

The webhook bot runs this sync on its own (``services/discord_roles.py``). This
script runs the same code once and prints every change, so the result can be
checked before the bot applies it, or a pass the bot held back (more removals
than ``DISCORD_ROLE_SYNC_MAX_REMOVALS``) can be pushed through.

Uses the webhook bot's token (``WEBHOOK_TOKEN``), because listing the server's
members needs the GUILD_MEMBERS intent and the core bot's application doesn't
have it.

Before ``scripts/seed_discord_roles.py`` has written the role map, the dry run
still works: it uses the roles that already exist and reports the ones not
created yet. Applying requires the map.

Usage:
    python scripts/sync_discord_roles.py                        # dry run
    python scripts/sync_discord_roles.py --apply                # make the changes
    python scripts/sync_discord_roles.py --apply --allow-mass-removal
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from interactions.api.http.http_client import HTTPClient  # noqa: E402

from services import discord_roles as dr  # noqa: E402


def _label(member: dict) -> str:
    user = member.get("user") or {}
    name = member.get("nick") or user.get("global_name") or user.get("username") or "?"
    return f"{name} ({user.get('id')})"


async def run(apply: bool, allow_mass_removal: bool) -> int:
    token = os.getenv("WEBHOOK_TOKEN")
    if not token:
        print("error: WEBHOOK_TOKEN not set", file=sys.stderr)
        return 2

    from db.models import Session

    with Session() as session:
        inputs = dr.load_role_inputs(session)
    desired = dr.desired_role_keys(inputs)

    http = HTTPClient()
    await http.login(token)
    try:
        members = await dr.fetch_guild_members(http)
        role_map = dr.load_role_map()
        missing = []
        if not role_map:
            resolved = dr.resolve_spec_roles(await http.get_roles(dr.MAIN_GUILD_ID), {}, dr.ALL_SPECS)
            role_map = {key: str(role["id"]) for key, role in resolved.items() if role}
            missing = [key for key, role in resolved.items() if not role]
            print("note: no role map yet (run scripts/seed_discord_roles.py); using existing roles")
            if missing:
                print(f"      not created yet, so not planned: {', '.join(missing)}")

        guard = None if allow_mass_removal else dr.max_removals_per_pass()
        plan = dr.plan_role_changes(desired, members, role_map, max_removals=guard)
        by_id = {str((m.get("user") or {}).get("id")): m for m in members}
        humans = sum(1 for m in members if not (m.get("user") or {}).get("bot"))

        print(f"members fetched: {len(members)} ({humans} people)")
        print(f"linked users owed a role: {len(desired)}; not on the server: {plan.not_in_guild}")
        holders = Counter()
        key_by_role = {rid: key for key, rid in role_map.items()}
        for m in members:
            for rid in m.get("roles") or ():
                if str(rid) in key_by_role:
                    holders[key_by_role[str(rid)]] += 1
        adds = Counter(key for _, key in plan.adds)
        removes = Counter(key for _, key in plan.removes + plan.held_back)
        print(f"{'role':<20} {'holders now':>11} {'add':>5} {'remove':>7}")
        for spec in dr.ALL_SPECS:
            state = "" if spec.key in role_map else "  (role not created)"
            print(f"{spec.name:<20} {holders[spec.key]:>11} {adds[spec.key]:>5} {removes[spec.key]:>7}{state}")

        for discord_id, key in plan.adds:
            print(f"  + {dr.SPECS_BY_KEY[key].name:<20} {_label(by_id.get(discord_id, {}))}")
        for discord_id, key in plan.removes + plan.held_back:
            print(f"  - {dr.SPECS_BY_KEY[key].name:<20} {_label(by_id.get(discord_id, {}))}")
        if plan.held_back:
            print(f"warning: {len(plan.held_back)} removals exceed the limit of {guard}; "
                  "they are held back (re-run with --allow-mass-removal to apply them)")

        if not apply:
            print("[DRY RUN] nothing changed")
            return 0
        if missing:
            print("error: seed the roles first (scripts/seed_discord_roles.py --apply)", file=sys.stderr)
            return 2
        stats = await dr.apply_role_plan(http, plan, role_map, op_limit=10_000)
        print(f"[APPLY] {stats}")
        return 1 if stats["errors"] or stats["forbidden"] else 0
    finally:
        await http.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    parser.add_argument("--allow-mass-removal", action="store_true",
                        help="apply removals even above DISCORD_ROLE_SYNC_MAX_REMOVALS")
    args = parser.parse_args()
    return asyncio.run(run(args.apply, args.allow_mass_removal))


if __name__ == "__main__":
    sys.exit(main())
