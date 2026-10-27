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
EDITABLE_TYPES = {"boolean", "int", "string", "text", "select", "channel", "role", "csv", "multi"}
#: Types the website draws with a picker of its own (a style gallery, boss and
#: pet pickers, a message-list editor). The plugin gets each as a type it
#: already draws: "multi" is a checklist of options. Values are converted both
#: ways here, so the stored form is always the registry's.
_AS_TYPE = {"boardstyle": "select", "messagelist": "text",
            "multiselect": "multi", "bosslist": "multi", "petlist": "multi"}
_PET_GROUPS = {"boss": "Boss pets", "skilling": "Skilling pets", "raids": "Raid pets",
               "clue": "Clue pets", "minigame": "Minigame pets", "misc": "Other pets"}
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


def _picker_options(s, stored_type: str, field: dict, cache: dict) -> list:
    """The choices for a field the website draws with a picker. Each list is
    built once per payload: seasonal copies share it."""
    if stored_type in cache:
        return cache[stored_type]
    if stored_type == "boardstyle":
        from web_api.routes.lootboard import selectable_styles

        out = [{"value": str(st["id"]), "label": st["name"], "group": st["category"]}
               for st in selectable_styles(s)]
    elif stored_type == "bosslist":
        from web_api.routes.config import pb_boss_names

        out = [{"value": n, "label": n, "group": None} for n in pb_boss_names()[0]]
    elif stored_type == "petlist":
        from utils.osrs_pets import PET_DISPLAY_BY_NORM, pet_categories, pets_in_category

        out, seen = [], set()
        for cat in pet_categories():
            for norm in sorted(pets_in_category(cat)):
                name = PET_DISPLAY_BY_NORM.get(norm)
                if name and name not in seen:
                    seen.add(name)
                    out.append({"value": name, "label": name, "group": _PET_GROUPS.get(cat, "Pets")})
    elif stored_type == "multiselect":
        out = [{"value": str(o["value"]), "label": o.get("label"), "group": None}
               for o in field.get("options") or []]
    else:
        out = []
    cache[stored_type] = out
    return out


def _to_client(stored_type, value):
    """A stored value in the shape the plugin's control edits."""
    import json

    if stored_type == "messagelist":
        try:
            entries = json.loads(value) if isinstance(value, str) and value.strip() else (value or [])
        except ValueError:
            entries = []
        return "\n".join(str(e) for e in entries) if isinstance(entries, list) else ""
    if stored_type == "petlist":
        try:
            parsed = json.loads(value) if isinstance(value, str) and value.strip().startswith("[") else None
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(p) for p in parsed]
    if stored_type in ("multiselect", "bosslist", "petlist"):
        if isinstance(value, list):
            return [str(v) for v in value]
        return [p.strip() for p in str(value or "").split(",") if p.strip()]
    return value


def _from_client(stored_type, value):
    """What the plugin sent, in the form the registry's writer accepts."""
    if stored_type == "messagelist":
        lines = value.splitlines() if isinstance(value, str) else []
        return [line.strip() for line in lines if line.strip()]
    if stored_type in ("multiselect", "bosslist", "petlist") and isinstance(value, list):
        picked = [str(v).strip() for v in value if str(v).strip()]
        return ",".join(picked) if stored_type == "bosslist" else picked
    return value


def _settings_payload(s, user_id: int, group, errors=None) -> dict:
    """The schema the plugin draws: every registry setting as a typed field."""
    from db.entitlements import HALL_OF_FAME_CONFIG_KEYS
    from web_api.config_registry import (
        CONFIG_CATEGORIES,
        MANAGED_KEYS,
        HARDCORE_PREFIX,
        SEASONAL_PREFIX,
        SENSITIVE_KEYS,
        all_config_keys,
        get_config_field,
    )
    from web_api.routes.config import load_group_config

    values = load_group_config(user_id, group.group_id)
    hof_locked = _hof_locked(s, user_id, group.group_id)
    cache = {}
    fields = []
    for key in all_config_keys():
        field = get_config_field(key)
        # Bot-managed message ids are nobody's to edit, so they aren't shown.
        if field is None or field["key"] in MANAGED_KEYS:
            continue
        seasonal = key.startswith(SEASONAL_PREFIX) and key != field["key"]
        # Hardcore mirrors join the two switches on the Hardcore Worlds page
        # instead of doubling every base category.
        hardcore = key.startswith(HARDCORE_PREFIX) and key != field["key"]
        stored_type = field.get("type")
        kind = "password" if key in SENSITIVE_KEYS else _AS_TYPE.get(stored_type, stored_type)
        editable = kind in EDITABLE_TYPES
        locked_reason = None
        if field["key"] in HALL_OF_FAME_CONFIG_KEYS and hof_locked:
            locked_reason = LOCKED_HOF
        options = []
        if stored_type in _AS_TYPE and editable:
            options = _picker_options(s, stored_type, field, cache)
        elif kind == "select":
            for opt in field.get("options") or []:
                if isinstance(opt, dict):
                    options.append({"value": str(opt.get("value")), "label": opt.get("label"), "group": None})
                else:
                    options.append({"value": str(opt), "label": _option_label(opt), "group": None})
        fields.append({
            "key": key,
            "label": field.get("label"),
            "help": (field.get("help") or "") + (" One message per line." if stored_type == "messagelist" else ""),
            "category": "seasonal" if seasonal else "hardcore" if hardcore else field.get("category"),
            "type": kind,
            # Never sent for sensitive keys or types drawn on the website only.
            "value": _to_client(stored_type, values.get(key)) if editable else None,
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
        if key in SENSITIVE_KEYS or _AS_TYPE.get(field.get("type"), field.get("type")) not in EDITABLE_TYPES:
            errors[key] = ON_WEBSITE
            continue
        if field["key"] in HALL_OF_FAME_CONFIG_KEYS:
            if hof_locked is None:
                hof_locked = _hof_locked(s, user_id, group_id)
            if hof_locked:
                errors[key] = LOCKED_HOF
                continue
        try:
            coerce_to_storage(key, _from_client(field.get("type"), value))
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
        from web_api.config_registry import get_config_field

        errors = _validate(s, user_id, group_id, changes)
        if not errors and changes:
            stored = {k: _from_client((get_config_field(k) or {}).get("type"), v) for k, v in changes.items()}
            try:
                apply_group_config_patch(user_id, group_id, stored)
            except ProblemException as e:
                if e.status == 403:
                    raise Refusal(403, "not_admin", NOT_ADMIN)
                # Checks only the website's writer makes (a lootboard style
                # that no longer exists, a taken group name) land on their key.
                culprits = [k for k in changes if k == "group_name"
                            or (get_config_field(k) or {}).get("type") == "boardstyle"]
                errors = {k: _problem_error(e) for k in (culprits or list(changes))}
        s.expire_all()
        return _settings_payload(s, user_id, group, errors), (422 if errors else 200)

    try:
        payload, status = await _run(work)
        return jsonify(payload), status
    except Refusal as e:
        return _refuse(e)


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #
MEMBERS_PER_PAGE = 100
HIDDEN_HELP = ("Hidden members still track their drops, but are left off your lootboard "
               "and leaderboards.")


def _ago(when) -> str:
    seconds = max(0, int((datetime.now() - when).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def _wom_note(s, group) -> dict:
    """What the Sync from WOM button says about the last sync."""
    from db import GroupConfiguration

    if not group.wom_id:
        return {"can_sync": False, "note": "Add your WiseOldMan group under WiseOldMan & API to sync members."}
    row = (s.query(GroupConfiguration.config_value)
           .filter(GroupConfiguration.group_id == group.group_id,
                   GroupConfiguration.config_key == "last_wom_sync").first())
    try:
        note = f"Last synced from WiseOldMan {_ago(datetime.fromisoformat(row[0]))}." if row and row[0] else None
    except ValueError:
        note = None
    return {"can_sync": True, "note": note or "Members come from your WiseOldMan group, synced every hour."}


def _member_page(rows, loot: dict, hidden: set, q: str, page: int) -> dict:
    """One page of ``rows`` [(player_id, name)], most loot first. ``rank`` is
    the place in the whole group, so a search keeps each member's position."""
    from utils.format import rsn_contains

    ranked = sorted(rows, key=lambda r: (-loot.get(r[0], 0), (r[1] or "").lower()))
    needle = q.strip().lower()
    matches = [(i + 1, pid, name) for i, (pid, name) in enumerate(ranked)
               if not needle or rsn_contains(name or "", needle)]
    pages = max(1, -(-len(matches) // MEMBERS_PER_PAGE))
    page = min(max(1, page), pages)
    start = (page - 1) * MEMBERS_PER_PAGE
    total, hidden_count = len(rows), len(hidden & {pid for pid, _ in rows})
    summary = f"{total:,} member{'s' if total != 1 else ''}"
    if hidden_count:
        summary += f", {hidden_count:,} hidden"
    return {
        "summary": summary,
        "help": HIDDEN_HELP,
        "rows": [{"player_id": pid, "name": name, "rank": rank,
                  "detail": f"{_gp(loot[pid])} gp this month" if loot.get(pid) else "No loot this month",
                  "hidden": pid in hidden}
                 for rank, pid, name in matches[start:start + MEMBERS_PER_PAGE]],
        "page": page,
        "pages": pages,
        "matches": len(matches),
        # Plugin 6.1.15 shows one page; this says when there's more to find.
        "footer": (f"Showing {MEMBERS_PER_PAGE} of {len(matches):,}. Search to find anyone else."
                   if len(matches) > MEMBERS_PER_PAGE else None),
    }


def _members_payload(s, group, q: str, page: int) -> dict:
    from db import IgnoredPlayer, Player
    from db.models import user_group_association
    from web_api.common import period_to_partition, player_month_totals

    hidden = {pid for (pid,) in s.query(IgnoredPlayer.player_id)
              .filter(IgnoredPlayer.group_id == group.group_id)}
    rows = (s.query(Player.player_id, Player.player_name)
            .join(user_group_association, user_group_association.c.player_id == Player.player_id)
            .filter(user_group_association.c.group_id == group.group_id)
            .distinct().all())
    totals = player_month_totals([pid for pid, _ in rows], period_to_partition(None)) if rows else {}
    payload = _member_page(rows, {pid: int(totals.get(pid) or 0) for pid, _ in rows}, hidden, q, page)
    payload["wom"] = _wom_note(s, group)
    return payload


def _page_arg() -> int:
    try:
        return int(request.args.get("page") or 1)
    except ValueError:
        return 1


@plugin_admin_bp.get("/plugin/admin/groups/<int:group_id>/members")
@rate_limit(limit=40, period=timedelta(seconds=60))
async def admin_members(group_id: int):
    player_name, acc_hash, _ = await _identity_args()
    q, page = (request.args.get("q") or "")[:40], _page_arg()

    def work(s):
        from db import Group

        _authorize(s, group_id, player_name, acc_hash)
        group = s.query(Group).filter(Group.group_id == group_id).first()
        return _members_payload(s, group, q, page)

    try:
        return jsonify(await _run(work)), 200
    except Refusal as e:
        return _refuse(e)


@plugin_admin_bp.post("/plugin/admin/groups/<int:group_id>/members/<int:player_id>/hidden")
@rate_limit(limit=40, period=timedelta(seconds=60))
async def admin_member_hidden(group_id: int, player_id: int):
    player_name, acc_hash, body = await _identity_args()
    hidden = body.get("hidden") is True

    def work(s):
        from db import AuditLog, IgnoredPlayer
        from db.models import user_group_association

        user_id, _ = _authorize(s, group_id, player_name, acc_hash)
        member = (s.query(user_group_association.c.player_id)
                  .filter(user_group_association.c.group_id == group_id,
                          user_group_association.c.player_id == player_id).first())
        if member is None:
            raise Refusal(409, "not_member", "That player isn't in this group any more. Refresh the list.")
        row = (s.query(IgnoredPlayer)
               .filter(IgnoredPlayer.group_id == group_id, IgnoredPlayer.player_id == player_id).first())
        if hidden and row is None:
            s.add(IgnoredPlayer(group_id=group_id, player_id=player_id))
        elif not hidden and row is not None:
            s.delete(row)
        s.add(AuditLog(actor_user_id=user_id, group_id=group_id,
                       action="group.member.hide" if hidden else "group.member.show",
                       target=f"player:{player_id}", before=None, after="plugin"))
        s.commit()
        return {"player_id": player_id, "hidden": hidden}

    try:
        return jsonify(await _run(work)), 200
    except Refusal as e:
        return _refuse(e)


@plugin_admin_bp.post("/plugin/admin/groups/<int:group_id>/wom-sync")
@rate_limit(limit=6, period=timedelta(seconds=60))
async def admin_wom_sync(group_id: int):
    player_name, acc_hash, _ = await _identity_args()

    def work(s):
        from db import Group

        _authorize(s, group_id, player_name, acc_hash)
        group = s.query(Group).filter(Group.group_id == group_id).first()
        if not group.wom_id:
            raise Refusal(409, "no_wom", _wom_note(s, group)["note"])
        return int(group.wom_id)

    try:
        wom_id = await _run(work)
    except Refusal as e:
        return _refuse(e)
    try:
        from db.ops import sync_group_from_wom_with_stats

        result = await sync_group_from_wom_with_stats(wom_id=wom_id)
    except Exception:
        return jsonify({"message": "WiseOldMan didn't answer. Try again in a minute.", "tone": "warn"}), 200
    if result.get("on_cooldown"):
        minutes = max(1, -(-int(result.get("cooldown_remaining_seconds") or 0) // 60))
        return jsonify({"message": f"Synced recently. You can sync again in {minutes} minute"
                        f"{'s' if minutes != 1 else ''}.", "tone": "warn"}), 200
    added, removed = len(result.get("added") or []), len(result.get("removed") or [])
    message = f"Synced: {added} joined, {removed} left, {int(result.get('total_members') or 0):,} members."
    if result.get("skipped_removals"):
        message += " WiseOldMan sent a partial list, so nobody was removed."
    return jsonify({"message": message, "tone": "good"}), 200


# --------------------------------------------------------------------------- #
# Registering a group (services/plugin_registration.py)
# --------------------------------------------------------------------------- #
def _redis():
    from utils.redis import redis_client

    return redis_client.client


@plugin_admin_bp.post("/plugin/register")
@rate_limit(limit=6, period=timedelta(seconds=60))
async def register_start():
    player_name, acc_hash, body = await _identity_args()
    if not player_name or not acc_hash:
        return jsonify({"error": NOT_REGISTERED, "code": "bad_request"}), 400
    wom_id = body.get("wom_id")

    def check(s):
        from db import Group

        if wom_id is not None and s.query(Group.group_id).filter(Group.wom_id == wom_id).first():
            raise Refusal(409, "wom_conflict", "That WiseOldMan group is already on the DropTracker.")

    from services import plugin_registration as reg

    try:
        if isinstance(wom_id, int):
            await _run(check)
            from utils.wiseoldman import check_group_by_id

            name, count, _ = await check_group_by_id(wom_id)
            if not name:
                raise reg.RegistrationError("WiseOldMan doesn't know that group. Check the id or link.", "wom")
            note = f"WiseOldMan group: {name} ({count or 0:,} members)."
        else:
            note = None
        out = reg.start(_redis(), acc_hash=str(acc_hash), player_name=player_name, name=body.get("name"),
                        wom_id=wom_id, roster=body.get("roster"))
    except Refusal as e:
        return jsonify({"error": e.message, "code": e.code, "field": "wom"}), e.status
    except reg.RegistrationError as e:
        return jsonify({"error": e.message, "code": "invalid", "field": e.field}), 422
    out["note"] = note
    return jsonify(out), 200


@plugin_admin_bp.get("/plugin/register/status")
@rate_limit(limit=40, period=timedelta(seconds=60))
async def register_status():
    from services import plugin_registration as reg

    return jsonify(reg.status(_redis(), request.args.get("id") or "", request.args.get("acc_hash") or "")), 200
