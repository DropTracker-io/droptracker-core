"""Which clans staff can invite to a staff-hosted clan-vs-clan event (web119a).

``GET /admin/event-invite-candidates`` lists every group with the numbers staff
filter on: members, members active this month (any loot tracked), this
month's loot (the lootboard's own total) and how many admins it has (a clan
with none has nobody to receive the invite).

This module is the pure half: filtering, sorting and shaping rows that the
route has already gathered. Stdlib only, so the unit tests load it directly.
"""
from __future__ import annotations

from typing import Iterable, Optional

#: Never offered: the site-wide "all players" group.
EXCLUDED_GROUP_IDS = frozenset({2})

SORT_KEYS = ("active", "members", "loot", "name")
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

#: Participant statuses that mean "already dealt with on this event".
ON_EVENT_STATUSES = ("invited", "accepted")


def count_members(pairs: Iterable[tuple], active_ids: set) -> dict:
    """``{group_id: (members, active)}`` from ``(group_id, player_id)`` pairs.
    Duplicate association rows (a known race) count once."""
    seen: dict[int, set] = {}
    for gid, pid in pairs:
        if gid is None or pid is None:
            continue
        seen.setdefault(int(gid), set()).add(int(pid))
    return {gid: (len(pids), len(pids & active_ids)) for gid, pids in seen.items()}


def _as_int(value, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    return str(value).lower() in ("1", "true", "yes", "on")


def parse_filters(args) -> dict:
    """Query-string filters -> a normalized dict (unknown sort -> 'active')."""
    sort = args.get("sort") or "active"
    return {
        "min_members": max(_as_int(args.get("min_members"), 0) or 0, 0),
        "min_active": max(_as_int(args.get("min_active"), 0) or 0, 0),
        "min_monthly_loot": max(_as_int(args.get("min_monthly_loot"), 0) or 0, 0),
        "require_admins": _as_bool(args.get("require_admins"), True),
        "require_guild": _as_bool(args.get("require_guild"), False),
        "exclude_on_event": _as_bool(args.get("exclude_on_event"), True),
        "q": (args.get("q") or "").strip().lower()[:60],
        "sort": sort if sort in SORT_KEYS else "active",
        "limit": min(max(_as_int(args.get("limit"), DEFAULT_LIMIT) or DEFAULT_LIMIT, 1),
                     MAX_LIMIT),
    }


def select_candidates(stats: list[dict], filters: dict,
                      event_status: Optional[dict] = None) -> dict:
    """Apply ``filters`` to per-group ``stats`` rows.

    Each stats row: ``group_id, group_name, icon_url, members, active,
    monthly_loot, admins, has_guild``. ``event_status`` maps group_id -> that
    clan's participant status on the event being filled (if any); each
    returned row carries it as ``event_status``. Returns
    ``{"rows": [...], "total": matches before the limit}``."""
    event_status = event_status or {}
    out = []
    for row in stats:
        gid = int(row["group_id"])
        if gid in EXCLUDED_GROUP_IDS:
            continue
        status = event_status.get(gid)
        if filters["exclude_on_event"] and status in ON_EVENT_STATUSES:
            continue
        if row["members"] < filters["min_members"]:
            continue
        if row["active"] < filters["min_active"]:
            continue
        if (row.get("monthly_loot") or 0) < filters["min_monthly_loot"]:
            continue
        if filters["require_admins"] and not row["admins"]:
            continue
        if filters["require_guild"] and not row["has_guild"]:
            continue
        if filters["q"] and filters["q"] not in (row.get("group_name") or "").lower():
            continue
        out.append({**row, "event_status": status})

    sort = filters["sort"]
    if sort == "name":
        out.sort(key=lambda r: ((r.get("group_name") or "").lower(), r["group_id"]))
    else:
        field = {"active": "active", "members": "members", "loot": "monthly_loot"}[sort]
        out.sort(key=lambda r: (-(r.get(field) or 0), r["group_id"]))
    return {"rows": out[: filters["limit"]], "total": len(out)}
