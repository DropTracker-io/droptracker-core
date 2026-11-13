"""Join-to-create voice rooms (suggestion #237): the pure half.

A group points ``vc_join_to_create`` at a voice channel (the "hub"). Joining
the hub makes the bot create a temporary voice channel next to it, named from
``vc_join_to_create_name``, and move the member into it. Anyone may follow
them in; the room is deleted once it is empty. The Discord shell is
``services/voice_rooms.py``; everything decidable without Discord lives here.

Rooms are recorded in Redis (not memory) so a room left behind by a restart,
or by a member who left while the bot was down, is still found and removed by
the sweep.
"""
from __future__ import annotations

import json
import time
from typing import Dict, Optional

HUB_KEY = "vc_join_to_create"
NAME_KEY = "vc_join_to_create_name"
DEFAULT_NAME = "{name}'s Channel"

#: Every live room: channel id -> json {guild_id, owner_id, hub_id, created_at}.
ROOMS_HASH = "voicerooms:rooms"
#: A member's current room in a guild, so a second hub join moves them back to
#: it instead of opening another: field "<guild_id>:<user_id>" -> channel id.
OWNERS_HASH = "voicerooms:owners"

#: Discord's channel-name limit.
NAME_MAX = 100
#: Rooms per guild. Each one is a real channel and a guild has 500.
MAX_ROOMS_PER_GUILD = 25
#: One room creation per member per this many seconds (hub hopping).
CREATE_COOLDOWN_S = 10
#: A room is never swept younger than this: it is empty for a moment between
#: being created and the member being moved in.
SWEEP_GRACE_S = 30


def render_room_name(template: Optional[str], display_name: str,
                     number: Optional[int] = None) -> str:
    """The room's channel name. ``{name}`` is the member's display name and
    ``{number}`` the room's slot in the guild ("01", "02", ...: the lowest
    number no live room holds, see :func:`next_room_number`), for groups
    that would rather not put a member's name on a channel.

    A template without either placeholder is used as written (every room
    then shares the name, which is allowed). Blank falls back to the default.
    """
    template = (template or "").strip() or DEFAULT_NAME
    name = (display_name or "").strip() or "Member"
    slot = f"{int(number):02d}" if number else "01"
    rendered = template.replace("{name}", name).replace("{number}", slot).strip()
    return (rendered or DEFAULT_NAME.replace("{name}", name))[:NAME_MAX]


def next_room_number(records, guild_id) -> int:
    """The lowest positive number no live room in ``guild_id`` carries, so
    rooms read "Channel 01, 02, 03" and a freed number is reused next."""
    used = set()
    for rec in records or ():
        if not isinstance(rec, dict) or str(rec.get("guild_id")) != str(guild_id):
            continue
        try:
            used.add(int(rec.get("number") or 0))
        except (TypeError, ValueError):
            continue
    n = 1
    while n in used:
        n += 1
    return n


def hub_map(rows) -> Dict[int, int]:
    """``{hub_channel_id: group_id}`` from ``(group_id, value)`` config rows.

    Values are channel snowflakes; the legacy ``'0'`` sentinel, blanks and
    free text (a channel *name* typed into a degraded picker) are ignored.
    Lowest group id wins a hub shared by two groups, as config reads do.
    """
    out: Dict[int, int] = {}
    for group_id, value in sorted(rows, key=lambda r: r[0]):
        raw = str(value or "").strip()
        if not raw.isdigit() or raw == "0":
            continue
        out.setdefault(int(raw), int(group_id))
    return out


def owner_field(guild_id, user_id) -> str:
    return f"{int(guild_id)}:{int(user_id)}"


def room_record(guild_id, owner_id, hub_id, now: Optional[float] = None,
                number: Optional[int] = None) -> str:
    rec = {"guild_id": str(guild_id), "owner_id": str(owner_id),
           "hub_id": str(hub_id), "created_at": int(now or time.time())}
    if number:
        rec["number"] = int(number)
    return json.dumps(rec)


def parse_record(raw) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        rec = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def sweepable(record: dict, member_count: int, now: Optional[float] = None) -> bool:
    """True when an empty room is old enough to delete."""
    if member_count > 0:
        return False
    age = (now or time.time()) - int(record.get("created_at") or 0)
    return age >= SWEEP_GRACE_S


# --------------------------------------------------------------------------- #
# Permission diagnosis: why a room could not be created, in words an admin can
# act on. Discord answers only "403 Missing Permissions"; a category that
# denies @everyone Manage Channels/Connect (common on voice categories) takes
# them from the bot too unless its role is allowed back, and a new room copies
# the category's overwrites, which Discord permits only for permission bits
# the bot itself holds (or with Manage Permissions there).
# --------------------------------------------------------------------------- #
ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
MANAGE_CHANNELS = 1 << 4
CONNECT = 1 << 20
MOVE_MEMBERS = 1 << 24
MANAGE_ROLES = 1 << 28  # "Manage Permissions" on a channel

_PERM_LABELS = (
    (VIEW_CHANNEL, "View Channel"),
    (MANAGE_CHANNELS, "Manage Channels"),
    (CONNECT, "Connect"),
    (MOVE_MEMBERS, "Move Members"),
)


def effective_permissions(guild_id, member_id, member_role_ids, roles: Dict[str, int],
                          overwrites) -> int:
    """Discord's permission resolution for one member in one channel.

    ``roles`` maps role id -> permission int (the @everyone role's id is the
    guild id); ``overwrites`` is the channel's raw overwrite list.
    """
    gid = str(guild_id)
    role_ids = [str(r) for r in (member_role_ids or [])]
    perms = int(roles.get(gid, 0))
    for rid in role_ids:
        perms |= int(roles.get(rid, 0))
    if perms & ADMINISTRATOR:
        return -1  # everything
    by_id = {str(o.get("id")): o for o in (overwrites or [])}
    if (ev := by_id.get(gid)) is not None:
        perms = (perms & ~int(ev.get("deny") or 0)) | int(ev.get("allow") or 0)
    allow = deny = 0
    for rid in role_ids:
        o = by_id.get(rid)
        if o is not None and int(o.get("type", 0)) == 0:
            allow |= int(o.get("allow") or 0)
            deny |= int(o.get("deny") or 0)
    perms = (perms & ~deny) | allow
    if (mo := by_id.get(str(member_id))) is not None:
        perms = (perms & ~int(mo.get("deny") or 0)) | int(mo.get("allow") or 0)
    return perms


def missing_room_permissions(perms: int, category_overwrites) -> list:
    """Labels of what the bot lacks to open rooms in a category ([] = none)."""
    if perms == -1:
        return []
    missing = [label for bit, label in _PERM_LABELS if not perms & bit]
    if not perms & MANAGE_ROLES:
        touched = 0
        for o in category_overwrites or []:
            touched |= int(o.get("allow") or 0) | int(o.get("deny") or 0)
        if touched & ~perms:
            missing.append("Manage Permissions")
    return missing


def permissions_notice(missing: list, category_name: Optional[str]) -> str:
    where = f"the **{category_name}** category" if category_name else "this server"
    return (
        "I couldn't open a voice room. In " + where + " I'm missing: **"
        + ", ".join(missing) + "**. Allow these for the DropTracker bot's role on "
        "the category (Edit Category > Permissions), then try again."
    )
