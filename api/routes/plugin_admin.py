"""Group administration from the RuneLite plugin (``/plugin/admin/...``).

Two factors stand behind every group call:

1. The account (player name + account hash, the plugin's usual identity) must
   belong to a site user who owns or administers the group, by the same rules
   the website uses (``web_api.deps.resolve_group_role``: the group_admins
   grant, else Manage Server on the linked Discord server while the owner
   allows that).
2. The request must carry a device session for that group
   (``Authorization: Bearer dtp_...``), which ``POST /plugin/admin/link``
   issues only in exchange for the group's verification code (web141a).
   Resetting or clearing the code revokes every session (web143a).

Settings are read and written through the website's own config functions
(``web_api.routes.config``), so a change made in game passes the same
validation, entitlement checks, audit log and cache invalidation as one made
on droptracker.io. The plugin draws the fields from the schema sent here and
evaluates nothing it receives.

Errors are ``{"error": <sentence for the leader>, "code": ...}`` with codes
``not_admin`` (403), ``wrong_code`` (401), ``session_expired`` (401),
``no_code`` (409) and ``throttled`` (429).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from quart import Blueprint, jsonify, request
from quart_rate_limiter import rate_limit

from api.core import get_db_session

plugin_admin_bp = Blueprint("plugin_admin", __name__)

#: Field types the plugin draws itself (GroupAdmin.KNOWN_TYPES). Anything else
#: is listed with a link to the website and cannot be changed from here.
EDITABLE_TYPES = {"boolean", "int", "string", "text", "select", "channel", "role", "csv"}
#: Channel kinds a setting can point at (categories are not messageable).
_CHANNEL_KINDS = {"text": "Channels", "forum": "Forums", "voice": "Voice channels", "thread": "Threads"}

NOT_ADMIN = ("This account isn't an owner or admin of that group. Claim your RSN on "
             "droptracker.io with the Discord account that manages it.")
NOT_REGISTERED = "This account isn't on the DropTracker yet. Submit a drop first, then try again."
SESSION_ENDED = "Your session for this group has ended. Enter the code again."
WRONG_CODE = "That code isn't right. Check it with your group's owner."
BAD_FORMAT = "Codes are nine digits, like 123-456-789."
NO_CODE = ("This group has no verification code yet. Its owner can create one under "
           "Roles & access on droptracker.io.")
THROTTLED = "Too many tries. Wait a few minutes and try again."
LOCKED_HOF = "Hall of Fame is part of a paid plan. Upgrade on droptracker.io to change it."
ON_WEBSITE = "Change this one on droptracker.io."
MANAGED = "The bot keeps track of this message itself. Use Re-post board on droptracker.io to start it over."


class Refusal(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _refuse(e: Refusal):
    return jsonify({"error": e.message, "code": e.code}), e.status


# --------------------------------------------------------------------------- #
# Identity and authorization
# --------------------------------------------------------------------------- #
async def _identity_args():
    """(player_name, acc_hash, body) from the query (GET) or JSON body."""
    if request.method == "GET":
        return request.args.get("player_name"), request.args.get("acc_hash"), {}
    body = await request.get_json(silent=True) or {}
    return body.get("player_name"), body.get("acc_hash"), body


def _account_user(s, player_name, acc_hash):
    """The site user id behind this game account. Hash first, then strict
    name + hash, like every plugin endpoint."""
    from db import Player

    if not player_name or not acc_hash:
        raise Refusal(400, "bad_request", NOT_REGISTERED)
    player = s.query(Player).filter(Player.account_hash == str(acc_hash)).first()
    if player is None:
        player = (s.query(Player)
                  .filter(Player.player_name == player_name, Player.account_hash == str(acc_hash))
                  .first())
    if player is None:
        raise Refusal(403, "not_admin", NOT_REGISTERED)
    # user_id 0 is a real account: never truth-test it.
    if player.user_id is None:
        raise Refusal(403, "not_admin", NOT_ADMIN)
    return int(player.user_id)


def _role(s, user_id: int, group_id: int):
    """'owner' | 'admin' | None, by the website's rules."""
    from web_api.deps import manageable_guild_ids, resolve_group_role

    role = resolve_group_role(s, user_id, group_id, manageable_guild_ids(user_id))
    return role if role in ("owner", "admin") else None


def _authorize(s, group_id: int, player_name, acc_hash):
    """(user_id, role) for a group call, or a Refusal."""
    from db import plugin_admin_sessions as sessions

    token = sessions.bearer(request.headers.get("Authorization"))
    row, reason = sessions.verify(s, token, group_id, str(acc_hash or ""))
    if row is None:
        s.commit()  # keeps an expiry revocation
        raise Refusal(401, "session_expired", SESSION_ENDED)
    user_id = _account_user(s, player_name, acc_hash)
    if user_id != int(row.user_id):
        # The account now belongs to someone else: this session is not theirs.
        sessions.revoke(s, row, "account")
        s.commit()
        raise Refusal(401, "session_expired", SESSION_ENDED)
    role = _role(s, user_id, group_id)
    if role is None:
        s.commit()
        raise Refusal(403, "not_admin", NOT_ADMIN)
    s.commit()
    return user_id, role


# --------------------------------------------------------------------------- #
# Payload pieces
# --------------------------------------------------------------------------- #
def _member_count(s, group_id: int) -> int:
    from sqlalchemy import func

    from db.models import user_group_association

    return int(s.query(func.count(func.distinct(user_group_association.c.player_id)))
               .filter(user_group_association.c.group_id == group_id).scalar() or 0)


def _tier_label(s, group_id: int):
    try:
        from db.entitlements import effective_group_subscription

        tier = effective_group_subscription(s, group_id)["tier"]
        return tier.name if tier is not None else None
    except Exception:
        return None


def _group_dict(s, group, role, linked) -> dict:
    return {
        "id": group.group_id,
        "name": group.group_name,
        "role": role,
        "linked": bool(linked),
        "member_count": _member_count(s, group.group_id),
        "tier": _tier_label(s, group.group_id),
        "icon_url": group.icon_url,
    }


def _managed_groups(s, user_id: int):
    """[(group, role)] the user owns or administers, by grant or Discord."""
    from db import Group, GroupAdmin
    from web_api.deps import discord_perms_grant_admin, manageable_guild_ids

    ids = {int(r[0]) for r in s.query(GroupAdmin.group_id).filter(GroupAdmin.user_id == user_id)}
    guilds = manageable_guild_ids(user_id)
    if guilds:
        for gid, guild_id in s.query(Group.group_id, Group.guild_id).filter(Group.guild_id.in_(list(guilds))):
            if discord_perms_grant_admin(s, gid):
                ids.add(int(gid))
    out = []
    for group in s.query(Group).filter(Group.group_id.in_(list(ids))).order_by(Group.group_name).all() if ids else []:
        role = _role(s, user_id, group.group_id)
        if role:
            out.append((group, role))
    return out


def _cache_json(key):
    import json

    from utils.redis import redis_client

    try:
        raw = redis_client.client.get(key)
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _channels(guild_id):
    if not guild_id:
        return []
    entries = _cache_json(f"guild:{guild_id}:channels")
    names = {str(c.get("id")): c.get("name") for c in entries}
    out = []
    for c in entries:
        kind = c.get("type")
        if kind not in _CHANNEL_KINDS:
            continue
        label = c.get("name") or str(c.get("id"))
        if kind == "thread" and names.get(str(c.get("parent_id"))):
            label = f"{names[str(c.get('parent_id'))]} › {label}"
        out.append({"value": str(c.get("id")), "label": label, "group": _CHANNEL_KINDS[kind]})
    return out


def _roles(guild_id):
    if not guild_id:
        return []
    return [{"value": str(r.get("id")), "label": r.get("name") or str(r.get("id")), "group": None}
            for r in _cache_json(f"guild:{guild_id}:roles")]


def _option_label(value) -> str:
    text = str(value).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _hof_locked(s, user_id: int, group_id: int) -> bool:
    try:
        from web_api.deps import is_superadmin, load_user
        from web_api.entitlements import resolve_group_entitlements

        user = load_user(s, user_id)
        if is_superadmin(user):
            return False
        return not resolve_group_entitlements(s, group_id, user=user).get("hall_of_fame")
    except Exception:
        return True


def _settings_payload(s, user_id: int, group, errors=None) -> dict:
    """The schema the plugin draws: every registry setting as a typed field."""
    from db.entitlements import HALL_OF_FAME_CONFIG_KEYS
    from web_api.config_registry import (
        CONFIG_CATEGORIES,
        MANAGED_KEYS,
        SEASONAL_PREFIX,
        SENSITIVE_KEYS,
        all_config_keys,
        get_config_field,
    )
    from web_api.routes.config import load_group_config

    values = load_group_config(user_id, group.group_id)
    hof_locked = _hof_locked(s, user_id, group.group_id)
    fields = []
    for key in all_config_keys():
        field = get_config_field(key)
        # Bot-managed message ids are nobody's to edit, so they aren't shown.
        if field is None or field["key"] in MANAGED_KEYS:
            continue
        seasonal = key.startswith(SEASONAL_PREFIX) and key != field["key"]
        kind = "password" if key in SENSITIVE_KEYS else field.get("type")
        editable = kind in EDITABLE_TYPES
        locked_reason = None
        if field["key"] in HALL_OF_FAME_CONFIG_KEYS and hof_locked:
            locked_reason = LOCKED_HOF
        options = []
        if kind == "select":
            for opt in field.get("options") or []:
                if isinstance(opt, dict):
                    options.append({"value": str(opt.get("value")), "label": opt.get("label"), "group": None})
                else:
                    options.append({"value": str(opt), "label": _option_label(opt), "group": None})
        fields.append({
            "key": key,
            "label": field.get("label"),
            "help": field.get("help"),
            "category": "seasonal" if seasonal else field.get("category"),
            "type": kind,
            # Never sent for sensitive keys or types drawn on the website only.
            "value": values.get(key) if editable else None,
            "options": options,
            "min": field.get("min"),
            "max": field.get("max"),
            "locked": locked_reason is not None,
            "locked_reason": locked_reason,
        })
    categories = [{"key": c["key"], "label": c["label"], "help": None} for c in CONFIG_CATEGORIES]
    categories.append({"key": "seasonal", "label": "Seasonal worlds",
                       "help": "The same settings for Leagues and Deadman worlds."})
    return {
        "categories": categories,
        "fields": fields,
        "channels": _channels(group.guild_id),
        "roles": _roles(group.guild_id),
        "errors": errors or {},
    }


def _problem_error(e):
    """A website ProblemException as one sentence."""
    return getattr(e, "detail", None) or getattr(e, "title", None) or "That change couldn't be saved."


def _validate(s, user_id: int, group_id: int, changes: dict) -> dict:
    """{key: sentence} for every change the plugin may not or cannot make."""
    from db.entitlements import HALL_OF_FAME_CONFIG_KEYS
    from web_api.config_registry import (
        MANAGED_KEYS,
        ConfigValidationError,
        SENSITIVE_KEYS,
        coerce_to_storage,
        get_config_field,
    )

    errors = {}
    hof_locked = None
    for key, value in changes.items():
        field = get_config_field(str(key))
        if field is None:
            errors[key] = "That setting doesn't exist any more. Reopen the window."
            continue
        if field["key"] in MANAGED_KEYS:
            errors[key] = MANAGED
            continue
        if key in SENSITIVE_KEYS or field.get("type") not in EDITABLE_TYPES:
            errors[key] = ON_WEBSITE
            continue
        if field["key"] in HALL_OF_FAME_CONFIG_KEYS:
            if hof_locked is None:
                hof_locked = _hof_locked(s, user_id, group_id)
            if hof_locked:
                errors[key] = LOCKED_HOF
                continue
        try:
            coerce_to_storage(key, value)
        except ConfigValidationError as e:
            errors[key] = _sentence(key, e.detail)
    return errors


def _sentence(key: str, detail) -> str:
    """The website's validation message without its leading 'key' name."""
    text = str(detail or "That value isn't allowed.")
    prefix = f"'{key}' "
    if text.startswith(prefix):
        text = text[len(prefix):]
        text = text[:1].upper() + text[1:]
    return text


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _run(work):
    """Run ``work(s)`` on a worker thread with its own session."""
    def _go():
        s = get_db_session()
        try:
            return work(s)
        finally:
            s.close()
    return asyncio.to_thread(_go)


@plugin_admin_bp.get("/plugin/admin/groups")
@rate_limit(limit=20, period=timedelta(seconds=60))
async def admin_groups():
    player_name, acc_hash, _ = await _identity_args()

    def work(s):
        from db import plugin_admin_sessions as sessions

        try:
            user_id = _account_user(s, player_name, acc_hash)
        except Refusal as e:
            if e.status == 400:
                raise
            return {"groups": []}
        linked = sessions.linked_group_ids(s, str(acc_hash), user_id)
        return {"groups": [_group_dict(s, g, role, g.group_id in linked) for g, role in _managed_groups(s, user_id)]}

    try:
        return jsonify(await _run(work)), 200
    except Refusal as e:
        return _refuse(e)


@plugin_admin_bp.post("/plugin/admin/link")
@rate_limit(limit=10, period=timedelta(seconds=60))
async def admin_link():
    player_name, acc_hash, body = await _identity_args()
    try:
        group_id = int(body.get("group_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Pick a group first.", "code": "bad_request"}), 400

    def work(s):
        from db import AuditLog, Group
        from db import group_verification_codes as codes
        from db import plugin_admin_sessions as sessions
        from utils.redis import redis_client

        user_id = _account_user(s, player_name, acc_hash)
        group = s.query(Group).filter(Group.group_id == group_id).first()
        # Rights before the code: a non-admin never gets to guess at all.
        role = _role(s, user_id, group_id) if group is not None else None
        if role is None:
            raise Refusal(403, "not_admin", NOT_ADMIN)
        outcome = codes.check_code(s, getattr(redis_client, "client", None), group_id,
                                   body.get("code"), f"acc:{acc_hash}")
        if outcome == "throttled":
            raise Refusal(429, "throttled", THROTTLED)
        if outcome == "none":
            raise Refusal(409, "no_code", NO_CODE)
        if outcome == "invalid":
            raise Refusal(401, "wrong_code", BAD_FORMAT)
        if outcome != "ok":
            raise Refusal(401, "wrong_code", WRONG_CODE)
        generation = sessions.current_generation(s, group_id)
        token = sessions.mint(s, group_id, user_id, str(acc_hash), generation)
        s.add(AuditLog(actor_user_id=user_id, group_id=group_id, action="plugin_admin.link",
                       target="plugin_admin_sessions", before=None, after=str(player_name)[:64]))
        s.commit()
        return {"token": token, "group": _group_dict(s, group, role, True)}

    try:
        return jsonify(await _run(work)), 200
    except Refusal as e:
        return _refuse(e)


@plugin_admin_bp.post("/plugin/admin/groups/<int:group_id>/unlink")
async def admin_unlink(group_id: int):
    _, acc_hash, _ = await _identity_args()

    def work(s):
        from db import plugin_admin_sessions as sessions

        token = sessions.bearer(request.headers.get("Authorization"))
        row, _ = sessions.verify(s, token, group_id, str(acc_hash or ""))
        if row is not None:
            sessions.revoke(s, row, "unlink")
        s.commit()
        return {"ok": True}

    return jsonify(await _run(work)), 200


@plugin_admin_bp.get("/plugin/admin/groups/<int:group_id>/overview")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def admin_overview(group_id: int):
    player_name, acc_hash, _ = await _identity_args()

    def work(s):
        from db import Group
        from services.group_loot_totals import board_month_total
        from web_api.routes.group_setup import _status

        _, role = _authorize(s, group_id, player_name, acc_hash)
        group = s.query(Group).filter(Group.group_id == group_id).first()
        status = _status(s, group)
        members = _member_count(s, group_id)
        tracking = next((c for c in status["checks"] if c["key"] == "plugin"), None)
        loot = board_month_total(group_id, datetime.now().strftime("%Y%m"))
        stats = [
            {"label": "Members", "value": f"{members:,}", "detail": None},
            {"label": "Loot this month", "value": _gp(loot) if loot is not None else "-", "detail": None},
            {"label": "Plan", "value": _tier_label(s, group_id) or "Free", "detail": None},
        ]
        if tracking and tracking["status"] == "ok":
            stats.insert(1, {"label": "Tracking", "value": tracking["title"].split(" ")[0],
                             "detail": "members this month"})
        checklist = [{"label": c["title"], "ok": c["status"] == "ok", "hint": c.get("detail")}
                     for c in status["checks"]]
        return {"group": _group_dict(s, group, role, True), "stats": stats, "checklist": checklist}

    try:
        return jsonify(await _run(work)), 200
    except Refusal as e:
        return _refuse(e)


def _gp(value: int) -> str:
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{value:,}"


@plugin_admin_bp.get("/plugin/admin/groups/<int:group_id>/settings")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def admin_settings(group_id: int):
    player_name, acc_hash, _ = await _identity_args()

    def work(s):
        from db import Group

        user_id, _ = _authorize(s, group_id, player_name, acc_hash)
        group = s.query(Group).filter(Group.group_id == group_id).first()
        return _settings_payload(s, user_id, group)

    try:
        return jsonify(await _run(work)), 200
    except Refusal as e:
        return _refuse(e)


@plugin_admin_bp.patch("/plugin/admin/groups/<int:group_id>/settings")
@rate_limit(limit=20, period=timedelta(seconds=60))
async def admin_save_settings(group_id: int):
    player_name, acc_hash, body = await _identity_args()
    changes = body.get("changes") if isinstance(body.get("changes"), dict) else {}

    def work(s):
        from db import Group
        from web_api.common import ProblemException
        from web_api.routes.config import apply_group_config_patch

        user_id, _ = _authorize(s, group_id, player_name, acc_hash)
        group = s.query(Group).filter(Group.group_id == group_id).first()
        errors = _validate(s, user_id, group_id, changes)
        if not errors and changes:
            try:
                apply_group_config_patch(user_id, group_id, changes)
            except ProblemException as e:
                if e.status == 403:
                    raise Refusal(403, "not_admin", NOT_ADMIN)
                # Checks only the website's writer makes (a lootboard style
                # that no longer exists, a taken group name) land on their key.
                culprits = [k for k in changes if k in ("loot_board_type", "seasonal_loot_board_type", "group_name")]
                errors = {k: _problem_error(e) for k in (culprits or list(changes))}
        s.expire_all()
        return _settings_payload(s, user_id, group, errors), (422 if errors else 200)

    try:
        payload, status = await _run(work)
        return jsonify(payload), status
    except Refusal as e:
        return _refuse(e)
