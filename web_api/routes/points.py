"""Custom group points system — web surface.

Admin (session + group admin; mutations also need the `custom_points` entitlement):
  GET    /api/v1/groups/{id}/points/settings
  PUT    /api/v1/groups/{id}/points/settings      { rules?, behavior? }
  GET    /api/v1/groups/{id}/points/mods
  POST   /api/v1/groups/{id}/points/mods          { item_id?, npc_id?, event_type, award, divisor, description? }
  PATCH  /api/v1/groups/{id}/points/mods/{modId}
  DELETE /api/v1/groups/{id}/points/mods/{modId}
  GET    /api/v1/groups/{id}/points/lists
  POST   /api/v1/groups/{id}/points/lists         { list_type, item_id?, npc_id?, npc_ids? }
  DELETE /api/v1/groups/{id}/points/lists/{entryId}
  GET    /api/v1/groups/{id}/points/item-sources?item_id=
  GET    /api/v1/groups/{id}/points/boosts
  POST   /api/v1/groups/{id}/points/boosts        { start_at, end_at, event_type, target_type, target_id?, operation, operation_value, description? }
  PATCH  /api/v1/groups/{id}/points/boosts/{boostId}
  DELETE /api/v1/groups/{id}/points/boosts/{boostId}
  GET    /api/v1/groups/{id}/points/seasons
  POST   /api/v1/groups/{id}/points/seasons       { name, start_at, end_at }
  PATCH  /api/v1/groups/{id}/points/seasons/{seasonId}
  DELETE /api/v1/groups/{id}/points/seasons/{seasonId}
  POST   /api/v1/groups/{id}/points/adjust        { player_id, amount, reason }
  GET    /api/v1/groups/{id}/points/history?player_id=&page=&limit=
  POST   /api/v1/groups/{id}/points/reset         { confirm: "RESET" }

Public (leaderboard visibility honors the `points_leaderboard_public` group
config; when the group opts out, members/admins can still view):
  GET /api/v1/groups/{id}/points/leaderboard?period=&page=&limit=

The awarding engine (`data/submissions/point_awards.py`) reads the same tables
these routes write: `group_point_settings`, `group_point_mods`,
`group_point_blacklist`, `group_point_events`, and behavior keys in
`group_configurations`. `group_point_seasons` is web-only (leaderboard windows).
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from quart import Blueprint, jsonify, request
from sqlalchemy import func

from db import Group, GroupConfiguration, ItemList, NpcList, Player, User
from db.models import (
    GroupPointBlacklist,
    GroupPointConfig,
    GroupPointMods,
    GroupPointSeason,
    GroupPointTimedEvent,
    PlayerPoints,
    user_group_association,
)
from db.models.analytics import Log
from web_api.common import (
    abort_problem,
    db_session,
    hidden_player_ids,
    parse_page,
    private_no_store,
    with_cache_headers,
)
from web_api.deps import (
    assert_group_admin,
    assert_group_entitlement,
    current_user_id,
    is_group_admin_role,
    json_body,
    load_user,
    manageable_guild_ids,
    optional_user_id,
    resolve_group_role,
)

points_bp = Blueprint("v1_points", __name__)

ENTITLEMENT_KEY = "custom_points"

# Base-rule reasons the engine understands. "drop" additionally uses a GP
# divisor; everything else is a flat award.
KNOWN_REASONS = (
    "drop", "pb", "pet", "clog",
    "easy_ca", "medium_ca", "hard_ca", "elite_ca", "master_ca", "grandmaster_ca",
)
# Values accepted for mod/boost event filters ("any" = wildcard).
EVENT_TYPES = ("any",) + KNOWN_REASONS

BEHAVIOR_BOOL_KEYS = (
    "stacks_award_points",
    "point_sharing",
    "points_require_group_only",
    "points_leaderboard_public",
)
BEHAVIOR_INT_KEYS = ("min_submission_pts", "max_submission_pts")
SHARING_METHODS = ("equal_split", "award_all")
BEHAVIOR_DEFAULTS = {
    "stacks_award_points": False,
    "point_sharing": False,
    "point_sharing_method": "equal_split",
    "points_require_group_only": False,
    "points_leaderboard_public": True,
    "min_submission_pts": 0,
    "max_submission_pts": 0,
}

ADMIN_MANUAL_ENTRY_TYPE = 99  # mirrors commands/group_admin.py
MAX_ADJUST = 1_000_000


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _assert_group_exists(s, group_id: int) -> Group:
    group = s.query(Group).filter(Group.group_id == group_id).first()
    if not group:
        abort_problem(404, "Group not found", f"No group {group_id}.")
    return group


def _admin_ctx(s, user_id: int, group_id: int, *, entitlement: bool = False):
    """Standard admin gate; optionally also requires the points entitlement."""
    _assert_group_exists(s, group_id)
    user = load_user(s, user_id)
    guild_ids = manageable_guild_ids(user_id)
    if entitlement:
        assert_group_entitlement(
            s, user_id, group_id, ENTITLEMENT_KEY,
            manage_guild_ids=guild_ids, user=user,
        )
    else:
        assert_group_admin(s, user_id, group_id, guild_ids, user=user)
    return user


def _config_row(s, group_id: int, key: str):
    return (
        s.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == key,
        )
        .first()
    )


def _read_behavior(s, group_id: int) -> dict:
    out = dict(BEHAVIOR_DEFAULTS)
    for key in BEHAVIOR_BOOL_KEYS:
        row = _config_row(s, group_id, key)
        if row is not None and row.config_value not in (None, ""):
            out[key] = str(row.config_value).strip() == "1"
    for key in BEHAVIOR_INT_KEYS:
        row = _config_row(s, group_id, key)
        if row is not None and row.config_value not in (None, ""):
            try:
                out[key] = max(0, int(row.config_value))
            except Exception:
                pass
    row = _config_row(s, group_id, "point_sharing_method")
    if row is not None and str(row.config_value or "").strip().lower() in SHARING_METHODS:
        out["point_sharing_method"] = str(row.config_value).strip().lower()
    return out


def _write_config(s, group_id: int, key: str, value: str) -> None:
    row = _config_row(s, group_id, key)
    if row is None:
        s.add(GroupConfiguration(group_id=group_id, config_key=key, config_value=value))
    else:
        row.config_value = value


def _rules_payload(s, group_id: int) -> list[dict]:
    rows = (
        s.query(GroupPointConfig)
        .filter(GroupPointConfig.group_id == group_id)
        .all()
    )
    if not rows:
        # Seed the engine's default rule set so the editor has rows to edit.
        from data.submissions.point_awards import _create_default_config

        _create_default_config(s, group_id, True)
        s.commit()
        rows = (
            s.query(GroupPointConfig)
            .filter(GroupPointConfig.group_id == group_id)
            .all()
        )
    order = {r: i for i, r in enumerate(KNOWN_REASONS)}
    rows.sort(key=lambda r: order.get(r.reason, 99))
    return [
        {
            "reason": r.reason,
            "award": int(r.award),
            "divisor": int(r.divisor),
            "uses_divisor": r.reason == "drop",
            "description": r.description or "",
        }
        for r in rows
    ]


def _name_maps(s, item_ids: set[int], npc_ids: set[int]) -> tuple[dict, dict]:
    items = {}
    npcs = {}
    if item_ids:
        for iid, name in (
            s.query(ItemList.item_id, ItemList.item_name)
            .filter(ItemList.item_id.in_(item_ids))
            .all()
        ):
            items[int(iid)] = name
    if npc_ids:
        for nid, name in (
            s.query(NpcList.npc_id, NpcList.npc_name)
            .filter(NpcList.npc_id.in_(npc_ids))
            .all()
        ):
            npcs[int(nid)] = name
    return items, npcs


def _parse_target_ids(body: dict, s) -> tuple:
    """Validate optional item_id / npc_id; at least one required."""
    item_id = body.get("item_id")
    npc_id = body.get("npc_id")
    try:
        item_id = int(item_id) if item_id not in (None, "", 0, "0") else None
    except Exception:
        abort_problem(400, "Invalid item", "item_id must be an integer.")
    try:
        npc_id = int(npc_id) if npc_id not in (None, "", 0, "0") else None
    except Exception:
        abort_problem(400, "Invalid NPC", "npc_id must be an integer.")
    if item_id is None and npc_id is None:
        abort_problem(400, "Target required", "Provide an item, an NPC, or both.")
    if item_id is not None and not s.query(ItemList.item_id).filter(ItemList.item_id == item_id).first():
        abort_problem(400, "Invalid item", f"No item with id {item_id}.")
    if npc_id is not None and not s.query(NpcList.npc_id).filter(NpcList.npc_id == npc_id).first():
        abort_problem(400, "Invalid NPC", f"No NPC with id {npc_id}.")
    return item_id, npc_id


def _parse_epoch(value, field: str) -> int:
    """ISO-8601 datetime → unix seconds, honoring an explicit UTC offset.

    Aware inputs ("…T13:00:00Z", "…+02:00") convert exactly; naive inputs
    keep the legacy behavior of being read in server-local time. User-defined
    instants (boosts, seasons) parse through this so admins get the precise
    moment they picked regardless of their timezone.
    """
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return int(datetime.fromisoformat(raw).timestamp())
    except Exception:
        abort_problem(400, "Invalid date", f"'{field}' must be an ISO-8601 datetime.")


def _parse_user_dt(value, field: str) -> datetime:
    """ISO-8601 datetime → naive-UTC datetime (offset honored, then dropped).

    For DateTime columns compared against server-written naive-UTC values
    (player_points.date_added et al). Naive-UTC, NOT naive-local: keep the
    stored domain independent of the box timezone.
    """
    return datetime.fromtimestamp(_parse_epoch(value, field), tz=timezone.utc).replace(tzinfo=None)


def _int_in_range(body: dict, key: str, lo: int, hi: int, default=None) -> int:
    raw = body.get(key, default)
    try:
        val = int(raw)
    except Exception:
        abort_problem(400, "Invalid value", f"'{key}' must be an integer.")
    if val < lo or val > hi:
        abort_problem(400, "Invalid value", f"'{key}' must be between {lo} and {hi}.")
    return val


def _season_payload(row: GroupPointSeason) -> dict:
    # Columns hold naive-UTC; emit aware ISO so browsers render the exact
    # instant in the viewer's local timezone (same contract as boosts).
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "id": row.id,
        "name": row.name,
        "start_at": row.start_at.replace(tzinfo=timezone.utc).isoformat() if row.start_at else None,
        "end_at": row.end_at.replace(tzinfo=timezone.utc).isoformat() if row.end_at else None,
        "active": bool(row.start_at and row.end_at and row.start_at <= now <= row.end_at),
    }


def _audit(s, *, action: str, actor: User | None, group: Group, message: str, details: dict) -> None:
    payload = dict(details)
    payload.update({
        "action": action,
        "actor_user_id": actor.user_id if actor else None,
        "actor_discord_id": str(actor.discord_id) if (actor and actor.discord_id) else None,
        "group_id": group.group_id,
        "group_name": group.group_name,
    })
    s.add(Log(
        level="INFO",
        source="group_admin_points",
        message=message,
        details=json.dumps(payload),
        timestamp=int(time.time()),
    ))


# --------------------------------------------------------------------------- #
# Settings (base rules + behavior)
# --------------------------------------------------------------------------- #
@points_bp.get("/groups/<int:group_id>/points/settings")
async def get_points_settings(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id)
            from web_api.entitlements import resolve_group_entitlements

            user = load_user(s, user_id)
            entitlements = resolve_group_entitlements(s, group_id, user=user)
            seasons = (
                s.query(GroupPointSeason)
                .filter(GroupPointSeason.group_id == group_id)
                .order_by(GroupPointSeason.start_at.desc())
                .all()
            )
            return {
                "enabled": bool(entitlements.get(ENTITLEMENT_KEY)),
                "rules": _rules_payload(s, group_id),
                "behavior": _read_behavior(s, group_id),
                "seasons": [_season_payload(row) for row in seasons],
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@points_bp.put("/groups/<int:group_id>/points/settings")
async def update_points_settings(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    rules_in = body.get("rules")
    behavior_in = body.get("behavior")

    def _apply():
        with db_session() as s:
            user = _admin_ctx(s, user_id, group_id, entitlement=True)
            group = _assert_group_exists(s, group_id)

            changed: dict = {}
            if isinstance(rules_in, list):
                existing = {
                    r.reason: r
                    for r in s.query(GroupPointConfig)
                    .filter(GroupPointConfig.group_id == group_id)
                    .all()
                }
                if not existing:
                    _rules_payload(s, group_id)  # seeds defaults
                    existing = {
                        r.reason: r
                        for r in s.query(GroupPointConfig)
                        .filter(GroupPointConfig.group_id == group_id)
                        .all()
                    }
                for rule in rules_in:
                    if not isinstance(rule, dict):
                        continue
                    reason = str(rule.get("reason") or "").strip()
                    row = existing.get(reason)
                    if row is None:
                        abort_problem(400, "Unknown rule", f"'{reason}' is not a configurable rule.")
                    award = _int_in_range(rule, "award", 0, 1_000_000, default=row.award)
                    row.award = award
                    changed.setdefault("rules", {})[reason] = {"award": award}
                    if reason == "drop" and "divisor" in rule:
                        divisor = _int_in_range(rule, "divisor", 1, 2_000_000_000)
                        row.divisor = divisor
                        changed["rules"][reason]["divisor"] = divisor

            if isinstance(behavior_in, dict):
                for key in BEHAVIOR_BOOL_KEYS:
                    if key in behavior_in:
                        val = bool(behavior_in[key])
                        _write_config(s, group_id, key, "1" if val else "0")
                        changed.setdefault("behavior", {})[key] = val
                for key in BEHAVIOR_INT_KEYS:
                    if key in behavior_in:
                        val = _int_in_range(behavior_in, key, 0, 1_000_000_000)
                        _write_config(s, group_id, key, str(val))
                        changed.setdefault("behavior", {})[key] = val
                if "point_sharing_method" in behavior_in:
                    method = str(behavior_in["point_sharing_method"] or "").strip().lower()
                    if method not in SHARING_METHODS:
                        abort_problem(400, "Invalid value", "point_sharing_method must be equal_split or award_all.")
                    _write_config(s, group_id, "point_sharing_method", method)
                    changed.setdefault("behavior", {})["point_sharing_method"] = method

            if changed:
                _audit(
                    s, action="settings_update", actor=user, group=group,
                    message=f"Points settings updated for {group.group_name}",
                    details={"changed": changed},
                )
            s.commit()
            return {
                "rules": _rules_payload(s, group_id),
                "behavior": _read_behavior(s, group_id),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


# --------------------------------------------------------------------------- #
# Per-item / per-NPC overrides (group_point_mods)
# --------------------------------------------------------------------------- #
def _mods_payload(s, group_id: int) -> list[dict]:
    rows = (
        s.query(GroupPointMods)
        .filter(GroupPointMods.group_id == group_id)
        .order_by(GroupPointMods.id.asc())
        .all()
    )
    items, npcs = _name_maps(
        s,
        {int(r.item_id) for r in rows if r.item_id},
        {int(r.npc_id) for r in rows if r.npc_id},
    )
    return [
        {
            "id": r.id,
            "item_id": int(r.item_id) if r.item_id else None,
            "item_name": items.get(int(r.item_id)) if r.item_id else None,
            "npc_id": int(r.npc_id) if r.npc_id else None,
            "npc_name": npcs.get(int(r.npc_id)) if r.npc_id else None,
            "event_type": r.event_type or "any",
            "award": int(r.award),
            "divisor": int(r.divisor),
            "description": r.description or "",
            "can_modify": bool(r.can_modify),
        }
        for r in rows
    ]


@points_bp.get("/groups/<int:group_id>/points/mods")
async def list_point_mods(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id)
            return {"mods": _mods_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@points_bp.post("/groups/<int:group_id>/points/mods")
async def create_point_mod(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            item_id, npc_id = _parse_target_ids(body, s)
            event_type = str(body.get("event_type") or "any").strip().lower()
            if event_type not in EVENT_TYPES:
                abort_problem(400, "Invalid value", f"event_type must be one of {', '.join(EVENT_TYPES)}.")
            award = _int_in_range(body, "award", 0, 1_000_000, default=1)
            divisor = _int_in_range(body, "divisor", 1, 2_000_000_000, default=1)
            description = str(body.get("description") or "").strip()[:255] or None
            row = GroupPointMods(
                group_id=group_id,
                item_id=item_id,
                npc_id=npc_id,
                event_type=event_type,
                award=award,
                divisor=divisor,
                can_modify=True,
                description=description,
            )
            s.add(row)
            s.commit()
            return {"id": row.id, "mods": _mods_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@points_bp.patch("/groups/<int:group_id>/points/mods/<int:mod_id>")
async def update_point_mod(group_id: int, mod_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            row = (
                s.query(GroupPointMods)
                .filter(GroupPointMods.id == mod_id, GroupPointMods.group_id == group_id)
                .first()
            )
            if row is None:
                abort_problem(404, "Not found", f"No override {mod_id} on this group.")
            if "award" in body:
                row.award = _int_in_range(body, "award", 0, 1_000_000)
            if "divisor" in body:
                row.divisor = _int_in_range(body, "divisor", 1, 2_000_000_000)
            if bool(row.can_modify):
                if "event_type" in body:
                    event_type = str(body.get("event_type") or "any").strip().lower()
                    if event_type not in EVENT_TYPES:
                        abort_problem(400, "Invalid value", f"event_type must be one of {', '.join(EVENT_TYPES)}.")
                    row.event_type = event_type
                if "item_id" in body or "npc_id" in body:
                    merged = {
                        "item_id": body.get("item_id", row.item_id),
                        "npc_id": body.get("npc_id", row.npc_id),
                    }
                    row.item_id, row.npc_id = _parse_target_ids(merged, s)
                if "description" in body:
                    row.description = str(body.get("description") or "").strip()[:255] or None
            s.commit()
            return {"mods": _mods_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@points_bp.delete("/groups/<int:group_id>/points/mods/<int:mod_id>")
async def delete_point_mod(group_id: int, mod_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            row = (
                s.query(GroupPointMods)
                .filter(GroupPointMods.id == mod_id, GroupPointMods.group_id == group_id)
                .first()
            )
            if row is None:
                abort_problem(404, "Not found", f"No override {mod_id} on this group.")
            if not bool(row.can_modify):
                abort_problem(403, "Locked", "This override is managed by DropTracker and cannot be removed.")
            s.delete(row)
            s.commit()
            return {"mods": _mods_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


# --------------------------------------------------------------------------- #
# Include / exclude lists (group_point_blacklist)
# --------------------------------------------------------------------------- #
LIST_TYPES = ("blacklist", "whitelist", "no_split")

#: Most rows one "add entry" call may create. An entry restricted to specific
#: drop sources becomes one row per source (see :func:`_parse_list_target`), and
#: a widely-sourced item can name hundreds of NPCs — past this the caller wants
#: "any source" (a single NULL-npc row), not a wall of rows.
MAX_LIST_ENTRY_SOURCES = 50


def _parse_list_target(body: dict, s) -> tuple[int | None, list[int]]:
    """Validate a list entry's target -> ``(item_id, npc_ids)``.

    Accepts ``npc_ids`` (a list) as well as the single ``npc_id``, because an
    item is normally blacklisted "from any of these sources": the matcher
    (``point_awards._point_list_entry_matches``) ANDs a row's item and npc, so
    that has to be one row per source rather than one row holding several ids.
    The caller expands accordingly.

    An EMPTY ``npc_ids`` means any source and stores a single NULL-npc row —
    which is also why the source picker only ever sends ids when the admin has
    deselected some: "all sources selected" must stay unrestricted so a source
    we have not observed yet (or an item recorded under a reward container
    rather than the boss) still matches.
    """
    item_id = body.get("item_id")
    try:
        item_id = int(item_id) if item_id not in (None, "", 0, "0") else None
    except Exception:
        abort_problem(400, "Invalid item", "item_id must be an integer.")

    raw_npcs = body.get("npc_ids")
    if raw_npcs in (None, ""):
        raw_npcs = [body.get("npc_id")]
    if not isinstance(raw_npcs, (list, tuple)):
        abort_problem(400, "Invalid NPC", "npc_ids must be a list of integers.")
    npc_ids: list[int] = []
    for raw in raw_npcs:
        if raw in (None, "", 0, "0"):
            continue
        try:
            value = int(raw)
        except Exception:
            abort_problem(400, "Invalid NPC", "npc_ids must be integers.")
        if value not in npc_ids:
            npc_ids.append(value)

    if item_id is None and not npc_ids:
        abort_problem(400, "Target required", "Provide an item, an NPC, or both.")
    if len(npc_ids) > MAX_LIST_ENTRY_SOURCES:
        abort_problem(
            400,
            "Too many sources",
            f"Pick at most {MAX_LIST_ENTRY_SOURCES} drop sources, "
            "or leave them all selected to match any source.",
        )
    if item_id is not None and not s.query(ItemList.item_id).filter(ItemList.item_id == item_id).first():
        abort_problem(400, "Invalid item", f"No item with id {item_id}.")
    if npc_ids:
        known = {
            int(n)
            for (n,) in s.query(NpcList.npc_id).filter(NpcList.npc_id.in_(npc_ids)).all()
        }
        missing = [n for n in npc_ids if n not in known]
        if missing:
            abort_problem(400, "Invalid NPC", f"No NPC with id {missing[0]}.")
    return item_id, npc_ids


def _lists_payload(s, group_id: int) -> list[dict]:
    rows = (
        s.query(GroupPointBlacklist)
        .filter(GroupPointBlacklist.group_id == group_id)
        .order_by(GroupPointBlacklist.id.asc())
        .all()
    )
    items, npcs = _name_maps(
        s,
        {int(r.item_id) for r in rows if r.item_id},
        {int(r.npc_id) for r in rows if r.npc_id},
    )
    return [
        {
            "id": r.id,
            "list_type": r.list_type or "blacklist",
            "item_id": int(r.item_id) if r.item_id else None,
            "item_name": items.get(int(r.item_id)) if r.item_id else None,
            "npc_id": int(r.npc_id) if r.npc_id else None,
            "npc_name": npcs.get(int(r.npc_id)) if r.npc_id else None,
        }
        for r in rows
    ]


@points_bp.get("/groups/<int:group_id>/points/lists")
async def list_point_lists(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id)
            return {"entries": _lists_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@points_bp.post("/groups/<int:group_id>/points/lists")
async def create_point_list_entry(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            list_type = str(body.get("list_type") or "").strip().lower()
            if list_type not in LIST_TYPES:
                abort_problem(400, "Invalid value", f"list_type must be one of {', '.join(LIST_TYPES)}.")
            item_id, npc_ids = _parse_list_target(body, s)
            # One row per chosen source; no sources = one unrestricted row.
            targets = [(item_id, n) for n in npc_ids] or [(item_id, None)]
            existing = {
                (int(r.item_id) if r.item_id else None, int(r.npc_id) if r.npc_id else None)
                for r in s.query(GroupPointBlacklist)
                .filter(
                    GroupPointBlacklist.group_id == group_id,
                    GroupPointBlacklist.list_type == list_type,
                )
                .all()
            }
            rows = [
                GroupPointBlacklist(
                    group_id=group_id,
                    list_type=list_type,
                    item_id=t_item,
                    npc_id=t_npc,
                )
                for (t_item, t_npc) in targets
                if (t_item, t_npc) not in existing
            ]
            if not rows:
                abort_problem(
                    409, "Already listed", "That target is already on this list."
                )
            for row in rows:
                s.add(row)
            s.commit()
            return {
                "id": rows[0].id,
                "ids": [r.id for r in rows],
                "entries": _lists_payload(s, group_id),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@points_bp.get("/groups/<int:group_id>/points/item-sources")
async def point_item_sources(group_id: int):
    """The NPCs an item is known to drop from — backs the list editor's
    "only from these sources" picker.

    Same resolver the event task form uses (wiki drop table + observed drops,
    alias groups collapsed), so both surfaces offer the same sources. Answers
    for the item's whole name-variant set, not the id passed in: a drop is
    recorded under whichever variant the client sent.
    """
    user_id = current_user_id()
    try:
        item_id = int(request.args.get("item_id") or 0)
    except Exception:
        abort_problem(400, "Invalid item", "item_id must be an integer.")
    if not item_id:
        abort_problem(400, "Item required", "Pass an item_id.")

    def _load():
        from db.item_sources import variant_item_ids

        with db_session() as s:
            _admin_ctx(s, user_id, group_id)
            row = (
                s.query(ItemList.item_name)
                .filter(ItemList.item_id == item_id)
                .first()
            )
            if row is None:
                abort_problem(400, "Invalid item", f"No item with id {item_id}.")
            item_name = row[0]
            ids = variant_item_ids(s, item_name, item_id)
        # _sources opens its own session — call it after this one has closed.
        from web_api.routes.items import _sources

        return {"item_id": item_id, "item_name": item_name, **_sources(ids)}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@points_bp.delete("/groups/<int:group_id>/points/lists/<int:entry_id>")
async def delete_point_list_entry(group_id: int, entry_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            row = (
                s.query(GroupPointBlacklist)
                .filter(
                    GroupPointBlacklist.id == entry_id,
                    GroupPointBlacklist.group_id == group_id,
                )
                .first()
            )
            if row is None:
                abort_problem(404, "Not found", f"No list entry {entry_id} on this group.")
            s.delete(row)
            s.commit()
            return {"entries": _lists_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


# --------------------------------------------------------------------------- #
# Timed boosts (group_point_events)
# --------------------------------------------------------------------------- #
OPERATIONS = ("multiply", "add", "set", "add_per_member")
TARGET_TYPES = ("any", "item", "npc")
MAX_BOOSTS_PER_GROUP = 100
MAX_BOOST_TARGETS = 25


def _boost_target_id_list(row) -> list[int]:
    """All target ids on a boost row: JSON target_ids first, scalar fallback."""
    raw = getattr(row, "target_ids", None)
    if raw:
        try:
            return [int(v) for v in json.loads(raw) if v is not None]
        except Exception:
            pass
    return [int(row.target_id)] if row.target_id else []


def _boosts_payload(s, group_id: int) -> list[dict]:
    rows = (
        s.query(GroupPointTimedEvent)
        .filter(GroupPointTimedEvent.group_id == group_id)
        .order_by(GroupPointTimedEvent.start_time_unix.desc())
        .all()
    )
    item_ids = {tid for r in rows if r.target_type == "item" for tid in _boost_target_id_list(r)}
    npc_ids = {tid for r in rows if r.target_type == "npc" for tid in _boost_target_id_list(r)}
    items, npcs = _name_maps(s, item_ids, npc_ids)
    now = int(time.time())
    out = []
    for r in rows:
        ids = _boost_target_id_list(r)
        names = items if r.target_type == "item" else npcs if r.target_type == "npc" else {}
        target_names = [names.get(tid) or f"#{tid}" for tid in ids]
        out.append({
            "id": r.id,
            # Aware UTC ISO ("+00:00") so browsers render the exact instant
            # in the viewer's local timezone instead of guessing.
            "start_at": datetime.fromtimestamp(int(r.start_time_unix), tz=timezone.utc).isoformat(),
            "end_at": datetime.fromtimestamp(int(r.end_time_unix), tz=timezone.utc).isoformat(),
            "event_type": r.event_type or "any",
            "target_type": r.target_type or "any",
            # Legacy scalar pair (single-target rows only) + the full lists.
            "target_id": ids[0] if len(ids) == 1 else None,
            "target_name": target_names[0] if len(ids) == 1 else None,
            "target_ids": ids,
            "target_names": target_names,
            "operation": r.operation or "multiply",
            "operation_value": int(r.operation_value),
            "description": r.description or "",
            "active": int(r.start_time_unix) <= now <= int(r.end_time_unix),
        })
    return out


def _validate_boost_body(s, body: dict, *, partial: bool = False) -> dict:
    out: dict = {}
    if not partial or "start_at" in body or "end_at" in body:
        if not partial or "start_at" in body:
            out["start_time_unix"] = _parse_epoch(body.get("start_at"), "start_at")
        if not partial or "end_at" in body:
            out["end_time_unix"] = _parse_epoch(body.get("end_at"), "end_at")
    if not partial or "event_type" in body:
        event_type = str(body.get("event_type") or "any").strip().lower()
        if event_type not in EVENT_TYPES:
            abort_problem(400, "Invalid value", f"event_type must be one of {', '.join(EVENT_TYPES)}.")
        out["event_type"] = event_type
    if not partial or "target_type" in body:
        target_type = str(body.get("target_type") or "any").strip().lower()
        if target_type not in TARGET_TYPES:
            abort_problem(400, "Invalid value", f"target_type must be one of {', '.join(TARGET_TYPES)}.")
        out["target_type"] = target_type

        # Collect requested targets: target_ids list preferred, scalar fallback.
        raw_ids = body.get("target_ids")
        if raw_ids in (None, ""):
            scalar = body.get("target_id")
            raw_ids = [] if scalar in (None, "", 0, "0") else [scalar]
        if not isinstance(raw_ids, list):
            abort_problem(400, "Invalid value", "target_ids must be a list of integers.")
        target_ids: list[int] = []
        for raw in raw_ids:
            try:
                tid = int(raw)
            except Exception:
                abort_problem(400, "Invalid value", "target_ids must be a list of integers.")
            if tid > 0 and tid not in target_ids:
                target_ids.append(tid)
        if len(target_ids) > MAX_BOOST_TARGETS:
            abort_problem(400, "Too many targets", f"A boost can target at most {MAX_BOOST_TARGETS} {target_type}s.")

        if target_type == "any" or not target_ids:
            out["target_id"] = None
            out["target_ids"] = None
        else:
            col = ItemList.item_id if target_type == "item" else NpcList.npc_id
            found = {int(row[0]) for row in s.query(col).filter(col.in_(target_ids)).all()}
            missing = [tid for tid in target_ids if tid not in found]
            if missing:
                abort_problem(400, "Invalid value", f"No {target_type} with id {missing[0]}.")
            # Canonical storage: single target stays on the scalar column.
            out["target_id"] = target_ids[0] if len(target_ids) == 1 else None
            out["target_ids"] = json.dumps(target_ids) if len(target_ids) > 1 else None
    if not partial or "operation" in body:
        operation = str(body.get("operation") or "multiply").strip().lower()
        if operation not in OPERATIONS:
            abort_problem(400, "Invalid value", f"operation must be one of {', '.join(OPERATIONS)}.")
        out["operation"] = operation
    if not partial or "operation_value" in body:
        out["operation_value"] = _int_in_range(body, "operation_value", 0, 1_000_000, default=1)
    if "description" in body:
        out["description"] = str(body.get("description") or "").strip()[:255] or None
    return out


@points_bp.get("/groups/<int:group_id>/points/boosts")
async def list_point_boosts(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id)
            return {"boosts": _boosts_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@points_bp.post("/groups/<int:group_id>/points/boosts")
async def create_point_boost(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            fields = _validate_boost_body(s, body)
            if fields["end_time_unix"] <= fields["start_time_unix"]:
                abort_problem(400, "Invalid window", "end_at must be after start_at.")
            existing = (
                s.query(func.count(GroupPointTimedEvent.id))
                .filter(GroupPointTimedEvent.group_id == group_id)
                .scalar()
            )
            if int(existing or 0) >= MAX_BOOSTS_PER_GROUP:
                abort_problem(400, "Too many boosts", f"A group can have at most {MAX_BOOSTS_PER_GROUP} timed boosts. Remove one first.")
            row = GroupPointTimedEvent(group_id=group_id, **fields)
            s.add(row)
            s.commit()
            return {"id": row.id, "boosts": _boosts_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@points_bp.patch("/groups/<int:group_id>/points/boosts/<int:boost_id>")
async def update_point_boost(group_id: int, boost_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            row = (
                s.query(GroupPointTimedEvent)
                .filter(
                    GroupPointTimedEvent.id == boost_id,
                    GroupPointTimedEvent.group_id == group_id,
                )
                .first()
            )
            if row is None:
                abort_problem(404, "Not found", f"No boost {boost_id} on this group.")
            fields = _validate_boost_body(s, body, partial=True)
            for key, value in fields.items():
                setattr(row, key, value)
            if int(row.end_time_unix) <= int(row.start_time_unix):
                abort_problem(400, "Invalid window", "end_at must be after start_at.")
            s.commit()
            return {"boosts": _boosts_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@points_bp.delete("/groups/<int:group_id>/points/boosts/<int:boost_id>")
async def delete_point_boost(group_id: int, boost_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            row = (
                s.query(GroupPointTimedEvent)
                .filter(
                    GroupPointTimedEvent.id == boost_id,
                    GroupPointTimedEvent.group_id == group_id,
                )
                .first()
            )
            if row is None:
                abort_problem(404, "Not found", f"No boost {boost_id} on this group.")
            s.delete(row)
            s.commit()
            return {"boosts": _boosts_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


# --------------------------------------------------------------------------- #
# Seasons (leaderboard windows)
# --------------------------------------------------------------------------- #
@points_bp.get("/groups/<int:group_id>/points/seasons")
async def list_point_seasons(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id)
            rows = (
                s.query(GroupPointSeason)
                .filter(GroupPointSeason.group_id == group_id)
                .order_by(GroupPointSeason.start_at.desc())
                .all()
            )
            return {"seasons": [_season_payload(r) for r in rows]}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@points_bp.post("/groups/<int:group_id>/points/seasons")
async def create_point_season(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            name = str(body.get("name") or "").strip()
            if not (1 <= len(name) <= 100):
                abort_problem(400, "Invalid name", "Season name must be 1–100 characters.")
            start = _parse_user_dt(body.get("start_at"), "start_at")
            end = _parse_user_dt(body.get("end_at"), "end_at")
            if end <= start:
                abort_problem(400, "Invalid window", "end_at must be after start_at.")
            row = GroupPointSeason(group_id=group_id, name=name, start_at=start, end_at=end)
            s.add(row)
            s.commit()
            return {"id": row.id, "season": _season_payload(row)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@points_bp.patch("/groups/<int:group_id>/points/seasons/<int:season_id>")
async def update_point_season(group_id: int, season_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            row = (
                s.query(GroupPointSeason)
                .filter(GroupPointSeason.id == season_id, GroupPointSeason.group_id == group_id)
                .first()
            )
            if row is None:
                abort_problem(404, "Not found", f"No season {season_id} on this group.")
            if "name" in body:
                name = str(body.get("name") or "").strip()
                if not (1 <= len(name) <= 100):
                    abort_problem(400, "Invalid name", "Season name must be 1–100 characters.")
                row.name = name
            if "start_at" in body:
                row.start_at = _parse_user_dt(body.get("start_at"), "start_at")
            if "end_at" in body:
                row.end_at = _parse_user_dt(body.get("end_at"), "end_at")
            if row.end_at <= row.start_at:
                abort_problem(400, "Invalid window", "end_at must be after start_at.")
            s.commit()
            return {"season": _season_payload(row)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@points_bp.delete("/groups/<int:group_id>/points/seasons/<int:season_id>")
async def delete_point_season(group_id: int, season_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id, entitlement=True)
            row = (
                s.query(GroupPointSeason)
                .filter(GroupPointSeason.id == season_id, GroupPointSeason.group_id == group_id)
                .first()
            )
            if row is None:
                abort_problem(404, "Not found", f"No season {season_id} on this group.")
            s.delete(row)
            s.commit()
            return {"ok": True}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


# --------------------------------------------------------------------------- #
# Manual adjustments, history, reset
# --------------------------------------------------------------------------- #
def _player_in_group(s, player_id: int, group_id: int) -> bool:
    return (
        s.query(user_group_association)
        .filter(
            user_group_association.c.player_id == player_id,
            user_group_association.c.group_id == group_id,
        )
        .first()
        is not None
    )


@points_bp.post("/groups/<int:group_id>/points/adjust")
async def adjust_points(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            user = _admin_ctx(s, user_id, group_id, entitlement=True)
            group = _assert_group_exists(s, group_id)
            amount = _int_in_range(body, "amount", -MAX_ADJUST, MAX_ADJUST)
            if amount == 0:
                abort_problem(400, "Invalid amount", "Amount must not be zero.")
            reason = str(body.get("reason") or "").strip()
            if not (3 <= len(reason) <= 120):
                abort_problem(400, "Invalid reason", "Reason must be 3–120 characters.")
            try:
                player_id = int(body.get("player_id"))
            except Exception:
                abort_problem(400, "Invalid player", "player_id must be an integer.")
            player = s.query(Player).filter(Player.player_id == player_id).first()
            if player is None:
                abort_problem(404, "Player not found", f"No player {player_id}.")
            if not _player_in_group(s, player_id, group_id):
                abort_problem(400, "Not a member", f"{player.player_name} is not a member of this group.")

            entry = PlayerPoints(
                player_id=player_id,
                group_id=group_id,
                amount=amount,
                reason=f"[Admin] {reason}"[:125],
                entry_type=ADMIN_MANUAL_ENTRY_TYPE,
            )
            s.add(entry)
            s.flush()
            _audit(
                s,
                action="add" if amount > 0 else "remove",
                actor=user,
                group=group,
                message=(
                    f"{'ADD' if amount > 0 else 'REMOVE'}: web user {user_id} adjusted "
                    f"{player.player_name}'s points by {amount:+d} in {group.group_name} "
                    f"(reason: {reason})"
                ),
                details={
                    "player_id": player_id,
                    "player_name": player.player_name,
                    "amount": amount,
                    "reason": reason,
                    "entry_id": entry.id,
                },
            )
            s.commit()

            total = (
                s.query(func.coalesce(func.sum(PlayerPoints.amount), 0))
                .filter(PlayerPoints.group_id == group_id, PlayerPoints.player_id == player_id)
                .scalar()
            )
            return {
                "entry_id": entry.id,
                "player_id": player_id,
                "player_name": player.player_name,
                "amount": amount,
                "new_total": int(total or 0),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@points_bp.get("/groups/<int:group_id>/points/history")
async def points_history(group_id: int):
    user_id = current_user_id()
    page, limit = parse_page(request, default_limit=25, max_limit=100)
    player_filter = request.args.get("player_id")
    only_manual = (request.args.get("manual") or "").strip() == "1"

    def _load():
        with db_session() as s:
            _admin_ctx(s, user_id, group_id)
            q = (
                s.query(PlayerPoints, Player.player_name)
                .join(Player, Player.player_id == PlayerPoints.player_id)
                .filter(PlayerPoints.group_id == group_id)
            )
            if player_filter:
                try:
                    q = q.filter(PlayerPoints.player_id == int(player_filter))
                except Exception:
                    abort_problem(400, "Invalid player", "player_id must be an integer.")
            if only_manual:
                q = q.filter(PlayerPoints.entry_type == ADMIN_MANUAL_ENTRY_TYPE)
            total = q.count()
            rows = (
                q.order_by(PlayerPoints.id.desc())
                .offset((page - 1) * limit)
                .limit(limit)
                .all()
            )
            entries = [
                {
                    "id": pp.id,
                    "player_id": pp.player_id,
                    "player_name": name,
                    "amount": int(pp.amount),
                    "reason": pp.reason or "",
                    "manual": pp.entry_type == ADMIN_MANUAL_ENTRY_TYPE,
                    "date": pp.date_added.isoformat() if pp.date_added else None,
                }
                for pp, name in rows
            ]
            return {
                "entries": entries,
                "meta": {"page": page, "limit": limit, "total": int(total)},
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@points_bp.post("/groups/<int:group_id>/points/reset")
async def reset_points(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            user = _admin_ctx(s, user_id, group_id)
            group = _assert_group_exists(s, group_id)
            if str(body.get("confirm") or "").strip().upper() != "RESET":
                abort_problem(400, "Confirmation required", 'Send { "confirm": "RESET" } to wipe all group points.')
            deleted = (
                s.query(PlayerPoints)
                .filter(PlayerPoints.group_id == group_id)
                .delete(synchronize_session=False)
            )
            _audit(
                s, action="reset", actor=user, group=group,
                message=f"Points reset for {group.group_name}: removed {deleted} award rows",
                details={"deleted_rows": int(deleted)},
            )
            s.commit()
            return {"deleted": int(deleted)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


# --------------------------------------------------------------------------- #
# Public leaderboard
# --------------------------------------------------------------------------- #
def _period_window(s, group_id: int, period: str):
    """Resolve a period param to (token, start, end). end is exclusive;
    (None, None) bounds mean all-time."""
    period = (period or "month").strip().lower()
    now = datetime.now()

    if period.startswith("season:"):
        try:
            season_id = int(period.split(":", 1)[1])
        except Exception:
            abort_problem(400, "Invalid period", "Season periods use the form season:<id>.")
        season = (
            s.query(GroupPointSeason)
            .filter(GroupPointSeason.id == season_id, GroupPointSeason.group_id == group_id)
            .first()
        )
        if season is None:
            abort_problem(404, "Season not found", f"No season {season_id} on this group.")
        return f"season:{season_id}", season.start_at, season.end_at

    if period == "all":
        return "all", None, None

    if period == "day" or (len(period) == 8 and period.isdigit()):
        if period == "day":
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            try:
                day = datetime.strptime(period, "%Y%m%d")
            except Exception:
                abort_problem(400, "Invalid period", f"Unrecognized day partition '{period}'.")
        return day.strftime("%Y%m%d"), day, day + timedelta(days=1)

    if period == "week" or (len(period) == 7 and period[4] == "w"):
        if period == "week":
            monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            try:
                year, week = int(period[:4]), int(period[5:])
                monday = datetime.fromisocalendar(year, week, 1)
            except Exception:
                abort_problem(400, "Invalid period", f"Unrecognized week partition '{period}'.")
        iso = monday.isocalendar()
        return f"{iso[0]}W{iso[1]:02d}", monday, monday + timedelta(days=7)

    # Month (default): "month" or YYYYMM.
    if period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        try:
            start = datetime.strptime(period, "%Y%m")
        except Exception:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.strftime("%Y%m"), start, end


@points_bp.get("/groups/<int:group_id>/points/leaderboard")
async def points_leaderboard(group_id: int):
    viewer_id = optional_user_id()
    period = request.args.get("period", "month")
    page, limit = parse_page(request, default_limit=25, max_limit=100)

    def _load():
        with db_session() as s:
            group = _assert_group_exists(s, group_id)

            behavior = _read_behavior(s, group_id)
            role = None
            if not behavior["points_leaderboard_public"]:
                if viewer_id is None:
                    abort_problem(403, "Leaderboard is private", "This group's points leaderboard is members-only.")
                role = resolve_group_role(s, viewer_id, group_id, manageable_guild_ids(viewer_id))
                if role is None:
                    abort_problem(403, "Leaderboard is private", "This group's points leaderboard is members-only.")

            token, start, end = _period_window(s, group_id, period)

            q = (
                s.query(
                    PlayerPoints.player_id,
                    func.sum(PlayerPoints.amount).label("points"),
                )
                .filter(PlayerPoints.group_id == group_id)
            )
            if start is not None:
                q = q.filter(PlayerPoints.date_added >= start)
            if end is not None:
                q = q.filter(PlayerPoints.date_added < end)
            q = q.group_by(PlayerPoints.player_id).having(func.sum(PlayerPoints.amount) != 0)

            total = q.count()
            rows = (
                q.order_by(func.sum(PlayerPoints.amount).desc(), PlayerPoints.player_id.asc())
                .offset((page - 1) * limit)
                .limit(limit)
                .all()
            )

            hidden = hidden_player_ids()
            ids = [int(pid) for pid, _ in rows if int(pid) not in hidden]
            name_map = {}
            if ids:
                name_map = dict(
                    s.query(Player.player_id, Player.player_name)
                    .filter(Player.player_id.in_(ids))
                    .all()
                )

            start_rank = (page - 1) * limit
            entries = []
            for pos, (pid, points) in enumerate(rows):
                pid = int(pid)
                if pid in hidden:
                    # Keep rank gaps rather than reshuffling (matches loot boards).
                    continue
                entries.append({
                    "rank": start_rank + pos + 1,
                    "id": pid,
                    "name": name_map.get(pid, f"Player {pid}"),
                    "points": int(points),
                })

            seasons = (
                s.query(GroupPointSeason)
                .filter(GroupPointSeason.group_id == group_id)
                .order_by(GroupPointSeason.start_at.desc())
                .all()
            )

            return {
                "period": token,
                "group_id": group_id,
                "group_name": group.group_name,
                "entries": entries,
                "seasons": [_season_payload(r) for r in seasons],
                "meta": {"page": page, "limit": limit, "total": int(total)},
            }

    resp = jsonify(await asyncio.to_thread(_load))
    return with_cache_headers(resp, max_age=15)
