#!/usr/bin/env python3
"""
Run one backfill pass of the reaction roles by hand
===================================================

The webhook bot grants these on its own (``services/reaction_roles.py``): live
on each reaction, and by a backfill at startup and every half hour. This script
runs that backfill once and lists everyone it would give the role to, and
checks the bot can actually grant it.

Uses the webhook bot's token (``WEBHOOK_TOKEN``), the same bot that runs it.

Usage:
    python scripts/sync_reaction_roles.py           # dry run
    python scripts/sync_reaction_roles.py --apply   # grant the roles
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

from services import reaction_roles as rr  # noqa: E402
from services.discord_roles import fetch_guild_members  # noqa: E402


async def _role_check(http, spec) -> str:
    """Whether the bot sits above the role, so Discord will let it grant it."""
    roles = {str(r["id"]): r for r in await http.get_roles(spec.guild_id)}
    role = roles.get(spec.role_id)
    if role is None:
        return "role not found on the server"
    me = await http.get_current_user()
    member = await http.get_member(spec.guild_id, me["id"])
    top = max((roles[str(r)]["position"] for r in member.get("roles") or () if str(r) in roles), default=0)
    perms = 0
    for rid in list(member.get("roles") or ()) + [spec.guild_id]:
        perms |= int((roles.get(str(rid)) or {}).get("permissions") or 0)
    manage = bool(perms & (1 << 28) or perms & (1 << 3))  # MANAGE_ROLES or ADMINISTRATOR
    above = top > role["position"]
    return (f"role '{role['name']}' at position {role['position']}; bot's top role at {top}; "
            f"manage roles: {'yes' if manage else 'NO'}; above the role: {'yes' if above else 'NO'}")


async def run(apply: bool) -> int:
    token = os.getenv("WEBHOOK_TOKEN")
    if not token:
        print("error: WEBHOOK_TOKEN not set", file=sys.stderr)
        return 2
    http = HTTPClient()
    await http.login(token)
    try:
        status = 0
        for spec in rr.REACTION_ROLES:
            print(f"== {spec.key}: message {spec.message_id} in channel {spec.channel_id}")
            print(f"   {await _role_check(http, spec)}")
            reactors = await rr.fetch_reactor_ids(http, spec)
            members = await fetch_guild_members(http, spec.guild_id)
            grants = rr.plan_grants(reactors, members, spec.role_id)
            by_id = {str((m.get("user") or {}).get("id")): m for m in members}
            on_server = sum(1 for r in set(reactors) if r in by_id)
            print(f"   reactors: {len(set(reactors))} ({on_server} still on the server); "
                  f"missing the role: {len(grants)}")
            for user_id in grants:
                m = by_id[user_id]
                user = m.get("user") or {}
                print(f"   + {m.get('nick') or user.get('global_name') or user.get('username')} ({user_id})")
            if apply:
                stats = await rr.reconcile(http, spec)
                print(f"   [APPLY] {stats}")
                if stats["forbidden"] or stats["error"]:
                    status = 1
        if not apply:
            print("[DRY RUN] nothing changed")
        return status
    finally:
        await http.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true", help="grant the roles (default: dry run)")
    return asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    sys.exit(main())
