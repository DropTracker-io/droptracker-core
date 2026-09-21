"""Undo interactions.py ``Role.edit`` resets on auto-created event team roles.

Until 2026-09-21 the team-discord reconciler renamed and recolored team roles
with ``role.edit(...)``. interactions 5.x Role.edit sends null for every
argument it wasn't given, and Discord treats null as "reset". So a recolor
also renamed the role to "new role", switched off mentionable (and hoist),
cleared any icon, and replaced its permissions with Discord's default role
set. That set grants Mention Everyone, Create Invite and server-wide
view/send, which a locked-down server's @everyone does not have. A rename
did the same, except for the name. The reconciler now PATCHes only
name/color (services/event_team_discord_bot._ensure_role).

For each live team row of the given events this restores what the bot set
when it created the role:

* ``name``: the team's name
* ``color``: the team's effective color (its accent, else the site palette)
* ``mentionable``: true (roles are created mentionable, for team pings)
* ``permissions``: the server's @everyone permissions (Discord's default for
  a role created without explicit permissions), when the role currently grants
  something @everyone does not

Discord accepted the null reset, but it will not let a bot add or remove a
permission the bot does not hold itself. So only the extra bits the bot holds
are removed, and any it cannot remove are listed for a server admin to clear
by hand.

Hoist and icon are left alone: the bot never set them, so their original
value is unknown and they are the server's to decide. Only fields that differ
are sent. Dry run by default; the core bot need not be stopped.

    venv/bin/python -m scripts.repair_team_role_resets --event 86           # dry run
    venv/bin/python -m scripts.repair_team_role_resets --event 86 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

REASON = "DropTracker: restore team role settings reset by a bot bug"


def _permission_names(bits: int) -> list:
    from interactions import Permissions

    return sorted(p.name for p in Permissions
                  if p.value and bin(p.value).count("1") == 1
                  and bits & p.value == p.value)


def _desired(session, row, team, event_id, roles_by_id, everyone_perms, bot_perms):
    """``(current role, patch, extra permission bits the bot cannot remove)``."""
    from services.event_team_discord import effective_team_color, team_icon_index

    current = roles_by_id.get(str(row.role_id))
    if current is None:
        return None, {}, 0
    index = team_icon_index(session, event_id, team.id)
    want = {
        "name": team.name[:100],
        "color": int(effective_team_color(team.color, index)[1:], 16),
        "mentionable": True,
    }
    patch = {k: v for k, v in want.items() if current.get(k) != v}
    perms = int(current.get("permissions") or 0)
    extra = perms & ~everyone_perms
    removable = extra if bot_perms & ADMINISTRATOR else extra & bot_perms
    if removable:
        patch["permissions"] = str(perms & ~removable)
    return current, patch, extra & ~removable


ADMINISTRATOR = 1 << 3


async def _bot_permissions(http, guild_id, bot_user_id, roles_by_id, everyone_perms) -> int:
    member = await http.get_member(guild_id, bot_user_id)
    perms = everyone_perms
    for role_id in member.get("roles", []):
        perms |= int((roles_by_id.get(str(role_id)) or {}).get("permissions") or 0)
    return perms


async def run(event_ids: list, apply: bool) -> int:
    from interactions.api.http.http_client import HTTPClient

    from db.models import EventTeam, EventTeamDiscord, Session

    token = os.getenv("BOT_TOKEN")
    if not token:
        print("error: BOT_TOKEN not set", file=sys.stderr)
        return 2
    session = Session()
    http = HTTPClient()
    bot_user_id = str((await http.login(token))["id"])
    try:
        rows = (session.query(EventTeamDiscord, EventTeam)
                .join(EventTeam, EventTeam.id == EventTeamDiscord.team_id)
                .filter(EventTeamDiscord.event_id.in_(event_ids),
                        EventTeamDiscord.role_id.isnot(None))
                .order_by(EventTeamDiscord.id.asc())
                .all())
        guild_roles: dict = {}
        changed = 0
        stuck_bits = 0
        for row, team in rows:
            guild_id = str(row.guild_id)
            if guild_id not in guild_roles:
                roles = await http.get_roles(guild_id)
                by_id = {str(r["id"]): r for r in roles}
                everyone = int((by_id.get(guild_id) or {}).get("permissions") or 0)
                guild_roles[guild_id] = (by_id, everyone, await _bot_permissions(
                    http, guild_id, bot_user_id, by_id, everyone))
            roles_by_id, everyone_perms, bot_perms = guild_roles[guild_id]
            current, patch, stuck = _desired(session, row, team, row.event_id,
                                             roles_by_id, everyone_perms, bot_perms)
            if stuck:
                stuck_bits |= stuck
                print(f"event {row.event_id} team {team.id} ({team.name!r}): the bot "
                      f"cannot remove {', '.join(_permission_names(stuck))} "
                      f"(it lacks them); a server admin must")
            if current is None:
                print(f"event {row.event_id} team {team.id} ({team.name!r}): "
                      f"role {row.role_id} not found in guild {guild_id}; skipped")
                continue
            if not patch:
                print(f"event {row.event_id} team {team.id} ({team.name!r}): "
                      f"role {row.role_id} already right")
                continue
            before = {k: current.get(k) for k in patch}
            print(f"event {row.event_id} team {team.id} ({team.name!r}): role "
                  f"{row.role_id} {before} -> {patch}")
            changed += 1
            if apply:
                await http.modify_guild_role(guild_id, row.role_id, patch,
                                             reason=REASON)
        mode = "Applied" if apply else "Dry run"
        print(f"{mode}: {changed} of {len(rows)} role(s) "
              f"{'restored' if apply else 'would be restored'}.")
        if stuck_bits:
            print("Left for a server admin (remove from the team roles by hand): "
                  + ", ".join(_permission_names(stuck_bits)))
        return 0
    finally:
        session.rollback()
        session.close()
        await http.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event", type=int, action="append", required=True,
                        help="event id whose team roles to restore (repeatable)")
    parser.add_argument("--apply", action="store_true",
                        help="send the PATCHes (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.event, args.apply)))


if __name__ == "__main__":
    main()
