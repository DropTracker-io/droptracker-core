#!/usr/bin/env python3
"""
Create, rename, style and order the tier and Bug Tester roles on the main server
================================================================================

Converges the roles in ``services/discord_roles.ROLE_SPECS``:

* adopts the roles that already exist (the unused "Supporter [T1]/[T2]/[T3]"
  become Supporter/Sponsor/Patron, the old "Supporter" becomes Fanatic, and
  "Bug Tester" stays itself), and creates the three "(Member)" roles;
* sets each role's name, colours (a gradient for subscribers where the server
  has Discord's enhanced role colours, else the flat first colour), hoist and
  icon. Existing image icons are kept, a Member role copies its tier's icon,
  and Bug Tester gets a Unicode bug;
* moves the block directly beneath the "━━━━━━━━" divider above the old
  Supporter role, in spec order, so the colours show over "Registered" and
  "Clan Leader";
* adopts "Registered" as it is and creates "Unregistered" directly beneath it
  (``STATUS_SPECS``: uncoloured, not hoisted, no permissions);
* writes ``data/discord_roles.json``, which switches the role sync on.

Runs with the core bot's token (``BOT_TOKEN``): its role is the highest of
our bots, and a bot can only edit roles beneath its own. Idempotent, so a
second ``--apply`` changes nothing unless someone edited a role in Discord.

Usage:
    python scripts/seed_discord_roles.py            # dry run: print what would change
    python scripts/seed_discord_roles.py --apply    # make the changes and write the map
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from interactions.api.http.http_client import HTTPClient  # noqa: E402

from services import discord_roles as dr  # noqa: E402

USER_AGENT = "DropTracker/1.0 (+https://www.droptracker.io; role setup)"
REASON = "DropTracker tier and Bug Tester roles"


def _hex(value) -> str:
    return "-" if value is None else f"#{int(value):06x}"


def _describe(patch: dict) -> str:
    parts = []
    for name, value in patch.items():
        if name == "colors":
            parts.append(f"colors={_hex(value['primary_color'])}"
                         + (f"->{_hex(value['secondary_color'])}" if value.get("secondary_color") else ""))
        elif name == "icon" and value:
            parts.append("icon=<copied image>")
        else:
            parts.append(f"{name}={value!r}")
    return ", ".join(parts)


def _icon_data_uri(role: dict) -> str:
    url = f"https://cdn.discordapp.com/role-icons/{role['id']}/{role['icon']}.png?size=256"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


async def _bot_top_position(http, guild_id: str, bot_user_id: str, roles: list) -> int:
    member = await http.get_member(guild_id, bot_user_id)
    positions = {str(r["id"]): int(r["position"]) for r in roles}
    return max((positions.get(str(rid), 0) for rid in member.get("roles") or ()), default=0)


async def _modify(http, guild_id: str, role_id: str, patch: dict) -> dict:
    """PATCH a role, retrying with a flat colour if Discord still refuses a gradient."""
    try:
        return await http.modify_guild_role(guild_id, role_id, patch, reason=REASON)
    except Exception as exc:
        colors = patch.get("colors") or {}
        if getattr(exc, "status", None) in (400, 403) and colors.get("secondary_color"):
            flat = dict(patch, colors=dict(colors, secondary_color=None))
            print(f"    gradient refused ({exc}); using a flat colour instead")
            return await http.modify_guild_role(guild_id, role_id, flat, reason=REASON)
        raise


async def _seed_status_roles(http, guild_id: str, role_map: dict, top: int, apply: bool) -> dict:
    """Adopt Registered untouched; create Unregistered and hold it directly beneath.

    Returns ``{key: role id}`` for the status roles that exist.
    """
    roles = await http.get_roles(guild_id)
    resolved = dr.resolve_spec_roles(roles, role_map, dr.STATUS_SPECS)
    registered, unregistered = resolved["registered"], resolved["unregistered"]
    if registered is None:
        print("  status 'Registered' not found; the status roles are left out of the sync")
        return {}
    if int(registered["position"]) >= top:
        print("  status 'Registered' is at or above the bot's highest role; left out of the sync")
        return {}
    print(f"  ok     {registered['name']!r} ({registered['id']}) adopted as is")
    if unregistered is None:
        spec = dr.SPECS_BY_KEY["unregistered"]
        print(f"  create {spec.name!r} beneath {registered['name']!r}")
        if not apply:
            return {"registered": str(registered["id"])}
        unregistered = await http.create_guild_role(guild_id, {
            "name": spec.name, "permissions": "0", "hoist": False,
            "mentionable": False, "color": 0,
        }, reason=REASON)
        roles = await http.get_roles(guild_id)
    changes = dr.plan_role_order(roles, [str(unregistered["id"])], str(registered["id"]), top)
    if changes is None:
        print("  order  'Unregistered' cannot be placed; left as is")
    elif not changes:
        print("  order  'Unregistered' already beneath 'Registered'")
    else:
        print("  order  move 'Unregistered' directly beneath 'Registered'")
        if apply:
            await http.modify_guild_role_positions(guild_id, changes, reason=REASON)
    return {"registered": str(registered["id"]), "unregistered": str(unregistered["id"])}


async def seed(apply: bool) -> int:
    token = os.getenv("BOT_TOKEN")
    if not token:
        print("error: BOT_TOKEN not set", file=sys.stderr)
        return 2
    guild_id = dr.MAIN_GUILD_ID
    http = HTTPClient()
    bot_user_id = str((await http.login(token))["id"])
    try:
        roles = await http.get_roles(guild_id)
        role_map = dr.load_role_map()
        resolved = dr.resolve_spec_roles(roles, role_map)
        top = await _bot_top_position(http, guild_id, bot_user_id, roles)
        print(f"guild {guild_id}: {len(roles)} roles; the bot's highest role is at position {top}")
        allow_gradient = dr.GRADIENT_FEATURE in ((await http.get_guild(guild_id, with_counts=False)).get("features") or [])
        if not allow_gradient:
            print(f"note: the server lacks {dr.GRADIENT_FEATURE}, so gradients fall back to their flat first colour")
        mode = "APPLY" if apply else "DRY RUN"
        print(f"[{mode}]")

        # 1. Create what is missing (at the bottom; step 3 moves it).
        for spec in dr.ROLE_SPECS:
            role = resolved[spec.key]
            if role is not None:
                if int(role["position"]) >= top:
                    print(f"error: role {role['name']!r} is at or above the bot's highest role", file=sys.stderr)
                    return 2
                continue
            print(f"  create {spec.name!r}")
            if apply:
                created = await http.create_guild_role(guild_id, {
                    "name": spec.name, "permissions": "0", "hoist": spec.hoist,
                    "mentionable": False, "color": spec.primary_color,
                }, reason=REASON)
                resolved[spec.key] = created
        if apply:
            roles = await http.get_roles(guild_id)
            by_id = {str(r["id"]): r for r in roles}
            resolved = {k: by_id.get(str(r["id"])) if r else None for k, r in resolved.items()}

        # 2. Name, colours, hoist, icon.
        for spec in dr.ROLE_SPECS:
            role = resolved[spec.key]
            current = role or {"name": None, "hoist": False, "colors": {}}
            patch = dr.role_patch(spec, current, allow_gradient)
            source = resolved.get(spec.icon_from) if spec.icon_from else None
            copy_icon = dr.icon_to_copy(spec, role, source)
            label = f"{role['name']!r} ({role['id']})" if role else f"{spec.name!r} (new)"
            if not patch and not copy_icon:
                print(f"  ok     {label}")
                continue
            summary = _describe(patch)
            if copy_icon == "emoji":
                summary = (summary + ", " if summary else "") + f"icon {source['unicode_emoji']} copied from {spec.icon_from!r}"
            elif copy_icon == "image":
                summary = (summary + ", " if summary else "") + f"image icon copied from {spec.icon_from!r}"
            print(f"  update {label}: {summary}")
            if apply and role is not None:
                if copy_icon == "emoji":
                    patch.update({"unicode_emoji": source["unicode_emoji"], "icon": None})
                elif copy_icon == "image":
                    patch.update({"icon": _icon_data_uri(source), "unicode_emoji": None})
                if patch:
                    await _modify(http, guild_id, str(role["id"]), patch)

        # 3. Order. Re-read: creating roles changed the list.
        if apply:
            roles = await http.get_roles(guild_id)
            by_id = {str(r["id"]): r for r in roles}
            resolved = {k: by_id.get(str(r["id"])) if r else None for k, r in resolved.items()}
            top = await _bot_top_position(http, guild_id, bot_user_id, roles)
        plan_roles = list(roles)
        missing = [s for s in dr.ROLE_SPECS if resolved[s.key] is None]
        for index, spec in enumerate(missing):
            # Dry run only (an apply has created them by now): Discord creates a
            # role at position 1, level with what is there and ranked below it.
            placeholder = {"id": str(10 ** 19 + index), "name": spec.name, "position": 1}
            plan_roles.append(placeholder)
            resolved[spec.key] = placeholder
        block = [str(resolved[s.key]["id"]) for s in dr.ROLE_SPECS]
        before = dr.current_role_order(plan_roles)
        changes = dr.plan_role_order(plan_roles, block, dr.ORDER_ANCHOR_ROLE_ID, top)
        if changes is None:
            print("  order  cannot be planned (divider missing, or a role at or above the bot); left as is")
        elif not changes:
            print("  order  already in place")
        else:
            print(f"  order  renumber all {len(changes)} roles to put the block beneath the divider "
                  "(every other role keeps its relative order)")
            if apply:
                await http.modify_guild_role_positions(guild_id, changes, reason=REASON)
            else:
                names = {str(r["id"]): r["name"] for r in plan_roles}
                renamed = {str(resolved[s.key]["id"]): s.name for s in dr.ROLE_SPECS}
                after = [c["id"] for c in changes]
                start = after.index(dr.ORDER_ANCHOR_ROLE_ID)
                print("  preview (top to bottom, from the divider):")
                for role_id in after[max(0, start - 1):start + len(dr.ROLE_SPECS) + 4]:
                    new_name = renamed.get(role_id)
                    print(f"    {names[role_id]!r}" + (f" -> {new_name!r}" if new_name and new_name != names[role_id] else ""))
        for spec in missing:
            resolved[spec.key] = None

        # 4. Registered / Unregistered: create Unregistered, keep it under Registered.
        status = await _seed_status_roles(http, guild_id, role_map, top, apply)

        # 5. Verify and record.
        if apply:
            roles = await http.get_roles(guild_id)
            final = {s.key: str(resolved[s.key]["id"]) for s in dr.ROLE_SPECS if resolved[s.key]}
            final.update(status)
            dr.write_role_map(final)
            print(f"wrote {dr.ROLE_MAP_PATH}")
            by_id = {str(r["id"]): r for r in roles}
            after = dr.current_role_order(roles)
            # The status roles are placed separately (step 4) and Unregistered
            # may not have existed when ``before`` was read.
            moved = set(block) | set(status.values())
            others_kept = [i for i in after if i not in moved] == [i for i in before if i not in moved]
            anchor = after.index(dr.ORDER_ANCHOR_ROLE_ID) if dr.ORDER_ANCHOR_ROLE_ID in after else None
            block_ok = anchor is not None and after[anchor + 1:anchor + 1 + len(block)] == block
            print("result (top to bottom, from the divider):")
            for role_id in (after[anchor:anchor + len(block) + 2] if anchor is not None else []):
                r = by_id[role_id]
                colors = r.get("colors") or {}
                print(f"  pos {int(r['position']):>3}  {r['name']!r:<22} {_hex(colors.get('primary_color', r.get('color')))}"
                      f"{'->' + _hex(colors['secondary_color']) if colors.get('secondary_color') else ''}"
                      f"  hoist={bool(r.get('hoist'))} icon={'image' if r.get('icon') else (r.get('unicode_emoji') or '-')}")
            print(f"every other role kept its relative order: {others_kept}")
            if not (block_ok and others_kept):
                print("warning: the order is not as planned; check it in Discord")
                return 1
        return 0
    finally:
        await http.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = parser.parse_args()
    return asyncio.run(seed(args.apply))


if __name__ == "__main__":
    sys.exit(main())
