"""Task 02 + Task 03 — current user identity and account settings.

  GET   /api/v1/me                     -> Me (identity, players, groups+role, is_superadmin)
  GET   /api/v1/me/settings            -> AccountSettings
  PATCH /api/v1/me                     -> AccountSettings (apply a subset, return full)
  PATCH /api/v1/me/players/{player_id} -> AccountSettings (toggle one account's visibility)
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify

from db import GroupAdmin, Player, User, UserConfiguration
from web_api.common import (
    abort_problem,
    db_session,
    money,
    period_to_partition,
    player_global_rank,
    player_month_total,
    private_no_store,
)
from web_api.flair import group_flairs
from web_api.deps import (
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
    resolve_group_role,
)
from web_api.routes.auth import get_cached_profile
from services.nitro_attribution import (
    NITRO_BOOST_CENTS,
    get_designated_group,
    pick_group_for_user,
    set_designated_group,
    user_group_ids,
)

me_bp = Blueprint("v1_me", __name__)


# Settings field -> User column mapping (Task 03 / AccountSettingsSchema).
# Only settings the backend actually enforces are exposed: `hidden` filters the
# user's accounts from public surfaces (leaderboards/search/profiles/feed), the
# ping trio gates Discord @-mentions in db.ops.get_formatted_name().
_BOOL_SETTINGS = [
    "hidden",
    "global_ping",
    "group_ping",
    "never_ping",
]
# Settings stored in user_configurations, shared with the Discord bot.
# `dm_account_changes` gates the RSN-change DM in data/submissions/common.py.
_CONFIG_SETTINGS = ["dm_account_changes"]
# Supporter submission-DM opt-ins (user_configurations), read by
# data/submissions/* at queue time. Saving them is allowed for everyone;
# they only take effect with the `dm_submissions` supporter entitlement.
_DM_CONFIG_SETTINGS = [
    "dm_drops",
    "dm_pbs",
    "dm_cas",
    "dm_clogs",
    "dm_pets",
    "dm_quests",
    "dm_deaths",
    "dm_diaries",
    "dm_levels",
]
# Minimum drop value (GP) for dm_drop DMs; stored as an int string.
_DM_MIN_VALUE_KEY = "dm_min_value"
_DM_MIN_VALUE_MAX = 2_147_483_647
# Set true by the bot when a DM bounces (Discord privacy settings); the site
# shows a fix-it banner. Users may PATCH it false to dismiss; the bot clears
# it automatically on the next successful DM.
_DM_DELIVERY_ISSUE_KEY = "dm_delivery_issue"


def _config_bool(s, user_id: int, key: str) -> bool:
    row = (
        s.query(UserConfiguration)
        .filter(UserConfiguration.user_id == user_id, UserConfiguration.config_key == key)
        .first()
    )
    return bool(row and str(row.config_value).lower() in ("true", "1"))


def _set_config_bool(s, user_id: int, key: str, value: bool) -> None:
    row = (
        s.query(UserConfiguration)
        .filter(UserConfiguration.user_id == user_id, UserConfiguration.config_key == key)
        .first()
    )
    if row:
        row.config_value = "true" if value else "false"
    else:
        s.add(UserConfiguration(user_id=user_id, config_key=key, config_value="true" if value else "false"))


def _config_value(s, user_id: int, key: str):
    row = (
        s.query(UserConfiguration)
        .filter(UserConfiguration.user_id == user_id, UserConfiguration.config_key == key)
        .first()
    )
    return row.config_value if row else None


def _set_config_value(s, user_id: int, key: str, value: str) -> None:
    row = (
        s.query(UserConfiguration)
        .filter(UserConfiguration.user_id == user_id, UserConfiguration.config_key == key)
        .first()
    )
    if row:
        row.config_value = value
    else:
        s.add(UserConfiguration(user_id=user_id, config_key=key, config_value=value))


def _settings_dict(s, user: User) -> dict:
    from web_api.entitlements import resolve_user_entitlements

    out = {k: bool(getattr(user, k, False)) for k in _BOOL_SETTINGS}
    for k in _CONFIG_SETTINGS + _DM_CONFIG_SETTINGS:
        out[k] = _config_bool(s, user.user_id, k)
    raw_min = _config_value(s, user.user_id, _DM_MIN_VALUE_KEY)
    try:
        out[_DM_MIN_VALUE_KEY] = int(raw_min) if raw_min else 0
    except (TypeError, ValueError):
        out[_DM_MIN_VALUE_KEY] = 0
    out[_DM_DELIVERY_ISSUE_KEY] = _config_bool(s, user.user_id, _DM_DELIVERY_ISSUE_KEY)
    out["supporter_entitlements"] = resolve_user_entitlements(s, user.user_id, user=user)
    out["players"] = [
        {"id": p.player_id, "name": p.player_name, "hidden": bool(p.hidden)}
        for p in s.query(Player).filter(Player.user_id == user.user_id).order_by(Player.player_name.asc()).all()
    ]
    return out


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
            # Flair for the user's subscribed groups (one query for all).
            group_flair_map = group_flairs(s, [g["id"] for g in groups])
            for g in groups:
                flair = group_flair_map.get(g["id"])
                if flair:
                    g["flair"] = flair

            from db.entitlements import resolve_user_entitlements as _resolve_supporter

            try:
                is_supporter = bool(_resolve_supporter(s, user.user_id).get("supporter_flair"))
            except Exception:
                is_supporter = False

            return {
                "user_id": user.user_id,
                "discord_id": str(user.discord_id or ""),
                "is_superadmin": bool(getattr(user, "is_superadmin", False)),
                "is_moderator": bool(getattr(user, "is_moderator", False)),
                "is_supporter": is_supporter,
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
            return _settings_dict(s, user) if user else None

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
        if key in _BOOL_SETTINGS or key in _CONFIG_SETTINGS or key in _DM_CONFIG_SETTINGS:
            if not isinstance(value, bool):
                abort_problem(422, "Invalid value", f"'{key}' must be a boolean.")
            updates[key] = value
        elif key == _DM_MIN_VALUE_KEY:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _DM_MIN_VALUE_MAX:
                abort_problem(422, "Invalid value", f"'{key}' must be a non-negative integer.")
            updates[key] = value
        elif key == _DM_DELIVERY_ISSUE_KEY:
            # Dismiss-only: the bot is the sole writer of `true`.
            if value is not False:
                abort_problem(422, "Invalid value", f"'{key}' can only be set to false (dismiss).")
            updates[key] = value
        else:
            abort_problem(422, "Unknown setting", f"'{key}' is not a settable field.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                return None
            for key, value in updates.items():
                if key == _DM_MIN_VALUE_KEY:
                    _set_config_value(s, user.user_id, key, str(value))
                elif key in _CONFIG_SETTINGS or key in _DM_CONFIG_SETTINGS or key == _DM_DELIVERY_ISSUE_KEY:
                    _set_config_bool(s, user.user_id, key, value)
                else:
                    setattr(user, key, value)
            s.commit()
            return _settings_dict(s, user)

    settings = await asyncio.to_thread(_apply)
    if settings is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return private_no_store(jsonify(settings))


@me_bp.patch("/me/players/<int:player_id>")
async def patch_my_player(player_id: int):
    """Toggle one linked account's public visibility (players.hidden) —
    the web equivalent of the bot's `/hideme <account>`."""
    user_id = current_user_id()
    body = await json_body()
    hidden = body.get("hidden")
    if not isinstance(hidden, bool):
        abort_problem(422, "Invalid value", "'hidden' must be a boolean.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                return None
            player = (
                s.query(Player)
                .filter(Player.player_id == player_id, Player.user_id == user.user_id)
                .first()
            )
            if not player:
                abort_problem(404, "Player not found", "That account is not linked to you.")
            player.hidden = hidden
            s.commit()
            return _settings_dict(s, user)

    settings = await asyncio.to_thread(_apply)
    if settings is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return private_no_store(jsonify(settings))


# --------------------------------------------------------------------------- #
# Nitro-boost group designation — which group a boost on the DropTracker
# Discord supports when the user belongs to more than one group. See
# services/nitro_attribution.py.
# --------------------------------------------------------------------------- #
def _nitro_payload(s, user_id: int) -> dict:
    from db import Group

    gids = user_group_ids(s, user_id)
    names: dict[int, str] = {}
    if gids:
        names = dict(
            s.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(gids))
            .all()
        )
    return {
        "per_boost_cents": NITRO_BOOST_CENTS,
        "designated_group_id": get_designated_group(s, user_id),
        # What the reconciler would credit right now (designation, else an
        # owned/admin group, else the lowest group_id).
        "effective_group_id": pick_group_for_user(s, user_id),
        "groups": [{"id": gid, "name": names.get(gid) or f"Group {gid}"} for gid in gids],
    }


@me_bp.get("/me/nitro-boost")
async def get_nitro_boost():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                return None
            return _nitro_payload(s, user_id)

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return private_no_store(jsonify(payload))


@me_bp.post("/me/nitro-boost")
async def set_nitro_boost():
    """Choose which of your groups a Nitro boost you place on the DropTracker
    Discord supports. Takes effect only while you are actually boosting; a
    single boost only ever credits one group."""
    user_id = current_user_id()
    body = await json_body()
    group_id = body.get("group_id")
    if group_id is not None and (isinstance(group_id, bool) or not isinstance(group_id, int)):
        abort_problem(422, "Invalid value", "'group_id' must be an integer or null.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                return None
            if group_id is not None and group_id not in user_group_ids(s, user_id):
                abort_problem(403, "Not a member", "You are not a member of that group.")
            set_designated_group(s, user_id, group_id)
            s.commit()
            return _nitro_payload(s, user_id)

    payload = await asyncio.to_thread(_apply)
    if payload is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return private_no_store(jsonify(payload))
