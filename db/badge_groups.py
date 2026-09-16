"""Groups whose members are decided by a badge instead of Wise Old Man.

``BADGE_GROUPS`` maps a group id to a badge key, as a JSON object
(``{"10000001": "bug_tester_helper"}``) or as ``id:key`` pairs separated by
commas. Every account of every user who holds that badge is a member, and
nobody else is.

It exists for the dev instance's Bug Testers group, which has no WOM group and
must never need one. Production leaves it unset, so nothing here runs there.

The rule runs in two places: at the end of the hourly membership sync
(``db.ops.update_group_members``), where group 2's "every player" rule
already runs, and straight after a tester roster is applied on dev
(``api/routes/dev_sync.py``), which is what makes a new tester a member
within seconds.

Only player rows are managed. A row linking a Discord *user* to the group
(``user_id`` set, ``player_id`` NULL) belongs to someone else and is left alone.
A group with a WOM id is never managed here, even if listed: two membership
sources for one group would fight each other every hour.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Set, Tuple

ENV = "BADGE_GROUPS"


def configured_badge_groups(raw: str = None) -> Dict[int, str]:
    """``{group_id: badge_key}`` from ``BADGE_GROUPS``. Malformed entries are dropped."""
    text = (os.getenv(ENV) if raw is None else raw) or ""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    if not text:
        return {}

    pairs = []
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        pairs = list(parsed.items())
    else:
        for part in text.replace(";", ",").split(","):
            if ":" in part:
                gid, key = part.split(":", 1)
                pairs.append((gid, key))

    out: Dict[int, str] = {}
    for gid, key in pairs:
        try:
            group_id = int(str(gid).strip().strip('"').strip("'"))
        except (TypeError, ValueError):
            continue
        badge_key = str(key or "").strip().strip('"').strip("'")
        # Groups 1 and 2 are the template and every-player groups.
        if group_id > 2 and badge_key:
            out[group_id] = badge_key
    return out


def desired_player_ids(session, badge_key: str) -> Set[int]:
    """Every account of every user holding an active award of an active ``badge_key``."""
    from db.models import Badge, Player, PlayerBadge

    holders = (
        session.query(Player.user_id)
        .join(PlayerBadge, PlayerBadge.player_id == Player.player_id)
        .join(Badge, Badge.badge_id == PlayerBadge.badge_id)
        .filter(
            Badge.key == badge_key,
            Badge.active == True,  # noqa: E712
            PlayerBadge.status == "active",
            Player.user_id.isnot(None),
        )
        .distinct()
        .all()
    )
    user_ids = sorted({int(uid) for (uid,) in holders})
    if not user_ids:
        return set()
    return {
        int(pid)
        for (pid,) in session.query(Player.player_id).filter(Player.user_id.in_(user_ids)).all()
    }


def sync_badge_group(session, group_id: int, badge_key: str) -> Tuple[int, int]:
    """Make the group's player rows match the badge. Returns ``(added, removed)``.

    Does not commit. A group that does not exist, or that is WOM-managed, is
    left untouched and reported as ``(0, 0)``.
    """
    from db.models import Group, user_group_association as uga

    group = session.get(Group, int(group_id))
    if group is None or group.wom_id not in (None, 0):
        return 0, 0

    wanted = desired_player_ids(session, badge_key)
    current = {
        int(pid)
        for (pid,) in session.query(uga.c.player_id)
        .filter(uga.c.group_id == group.group_id, uga.c.player_id.isnot(None))
        .all()
    }
    to_add = sorted(wanted - current)
    to_remove = sorted(current - wanted)
    if to_add:
        session.execute(
            uga.insert(),
            [{"player_id": pid, "user_id": None, "group_id": group.group_id} for pid in to_add],
        )
    if to_remove:
        session.execute(
            uga.delete().where(uga.c.group_id == group.group_id, uga.c.player_id.in_(to_remove))
        )
    return len(to_add), len(to_remove)


def sync_configured(session, commit: bool = True) -> Dict[int, Tuple[int, int]]:
    """Run :func:`sync_badge_group` for every configured group."""
    results: Dict[int, Tuple[int, int]] = {}
    groups = configured_badge_groups()
    if not groups:
        return results
    try:
        for group_id, badge_key in groups.items():
            results[group_id] = sync_badge_group(session, group_id, badge_key)
        if commit:
            session.commit()
    except Exception:
        if commit:
            session.rollback()
        raise
    return results
