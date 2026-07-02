"""Task 02 + Task 03 — current user identity and account settings.

  GET   /api/v1/me            -> Me (identity, players, groups+role, is_superadmin)
  GET   /api/v1/me/settings   -> AccountSettings
  PATCH /api/v1/me            -> AccountSettings (apply a subset, return full)
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify

from db import GroupAdmin, Player, User
from web_api.common import (
    abort_problem,
    db_session,
    money,
    period_to_partition,
    player_global_rank,
    player_month_total,
    private_no_store,
)
from web_api.deps import (
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
    resolve_group_role,
)
from web_api.routes.auth import get_cached_profile

me_bp = Blueprint("v1_me", __name__)


# Settings field -> User column mapping (Task 03 / AccountSettingsSchema).
_BOOL_SETTINGS = [
    "public",
    "hidden",
    "global_ping",
    "group_ping",
    "never_ping",
    "dm_on_rank_change",
    "dm_on_points",
    "update_logs_opt_in",
]
_GROUP_SETTINGS = ["patreon_group", "premium_group"]


def _settings_dict(user: User) -> dict:
    out = {k: bool(getattr(user, k, False)) for k in _BOOL_SETTINGS}
    for k in _GROUP_SETTINGS:
        val = getattr(user, k, None)
        out[k] = int(val) if val is not None else None
    return out


def _user_group_ids(user: User) -> set:
    try:
        return {g.group_id for g in user.groups}
    except Exception:
        return set()


@me_bp.get("/me")
async def get_me():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                return None
            partition = period_to_partition("all")
            manage_ids = manageable_guild_ids(user_id)

            # Players owned by this user.
            players = []
            for p in s.query(Player).filter(Player.user_id == user_id).all():
                loot = player_month_total(p.player_id, partition)
                rank = player_global_rank(p.player_id, partition)
                entry = {"id": p.player_id, "name": p.player_name, "total_loot": money(loot)}
                if rank is not None:
                    entry["global_rank"] = rank
                players.append(entry)

            # Groups: union of memberships + admin grants + MANAGE_GUILD guilds.
            group_roles: dict[int, str] = {}
            group_names: dict[int, str] = {}
            for g in user.groups:
                group_names[g.group_id] = g.group_name
            for ga in s.query(GroupAdmin).filter(GroupAdmin.user_id == user_id).all():
                group_names.setdefault(ga.group_id, None)
            for gid in list(group_names.keys()):
                role = resolve_group_role(s, user_id, gid, manage_ids, user=user)
                if role:
                    group_roles[gid] = role
                    if group_names.get(gid) is None:
                        grp = _lookup_group_name(s, gid)
                        group_names[gid] = grp

            groups = [
                {"id": gid, "name": group_names.get(gid) or f"Group {gid}", "role": role}
                for gid, role in group_roles.items()
            ]

            return {
                "user_id": user.user_id,
                "discord_id": str(user.discord_id or ""),
                "is_superadmin": bool(getattr(user, "is_superadmin", False)),
                "players": players,
                "groups": groups,
            }

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")

    profile = get_cached_profile(user_id)
    if profile.get("display_name"):
        payload["display_name"] = profile["display_name"]
    if profile.get("avatar_url"):
        payload["avatar_url"] = profile["avatar_url"]
    if profile.get("discord_id") and not payload.get("discord_id"):
        payload["discord_id"] = profile["discord_id"]

    return private_no_store(jsonify(payload))


def _lookup_group_name(s, group_id: int):
    from db import Group

    g = s.query(Group).filter(Group.group_id == group_id).first()
    return g.group_name if g else None


@me_bp.get("/me/settings")
async def get_settings():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            return _settings_dict(user) if user else None

    settings = await asyncio.to_thread(_load)
    if settings is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return private_no_store(jsonify(settings))


@me_bp.patch("/me")
async def patch_me():
    user_id = current_user_id()
    body = await json_body()

    # Validate keys/types up-front (reject unknown keys silently ignored? -> 422).
    updates = {}
    for key, value in body.items():
        if key in _BOOL_SETTINGS:
            if not isinstance(value, bool):
                abort_problem(422, "Invalid value", f"'{key}' must be a boolean.")
            updates[key] = value
        elif key in _GROUP_SETTINGS:
            if value is not None and not isinstance(value, int):
                abort_problem(422, "Invalid value", f"'{key}' must be an integer group id or null.")
            updates[key] = value
        else:
            abort_problem(422, "Unknown setting", f"'{key}' is not a settable field.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                return None
            member_ids = _user_group_ids(user)
            for key, value in updates.items():
                if key in _GROUP_SETTINGS and value is not None and value not in member_ids:
                    abort_problem(
                        422,
                        "Invalid group",
                        f"You are not a member of group {value}.",
                    )
                setattr(user, key, value)
            s.commit()
            return _settings_dict(user)

    settings = await asyncio.to_thread(_apply)
    if settings is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return private_no_store(jsonify(settings))
