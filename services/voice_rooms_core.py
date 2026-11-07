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


def render_room_name(template: Optional[str], display_name: str) -> str:
    """The room's channel name. ``{name}`` is the member's display name.

    A template without ``{name}`` is used as written (every room then shares
    the name, which is allowed). Blank falls back to the default.
    """
    template = (template or "").strip() or DEFAULT_NAME
    name = (display_name or "").strip() or "Member"
    rendered = template.replace("{name}", name).strip()
    return (rendered or DEFAULT_NAME.replace("{name}", name))[:NAME_MAX]


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


def room_record(guild_id, owner_id, hub_id, now: Optional[float] = None) -> str:
    return json.dumps({"guild_id": str(guild_id), "owner_id": str(owner_id),
                       "hub_id": str(hub_id), "created_at": int(now or time.time())})


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
