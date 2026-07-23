"""Request-scoped dependencies for the Web API v1 (Task 02).

Because Quart has no FastAPI-style DI, these are plain helpers called from
within a route. Auth reads the ``dt_session`` cookie the BFF forwards; role
derivation (§7.2) combines the new ``group_admins`` table with live Discord
MANAGE_GUILD membership cached at login.

DB-touching helpers take a Session so they can run inside the route's
``asyncio.to_thread`` block (keeping the event loop free), consistent with the
rest of the Web API.
"""
from __future__ import annotations

import json
import os
from typing import Optional, Set

from quart import request

from db import Group, GroupAdmin, GroupEventManager, User
from utils.redis import redis_client
from web_api.common import abort_problem
from web_api.session import verify_session

SESSION_COOKIE = "dt_session"

# MANAGE_GUILD permission bit (Discord). Presence => can manage the guild.
_MANAGE_GUILD = 0x20

_GUILDS_CACHE_PREFIX = "web:guilds:"          # set of manageable guild ids per user
# The guilds a user can manage are captured once at login (that is the only point
# we hold the Discord access token). Role derivation resolves "owner via
# MANAGE_GUILD" from this cache on every request, so it must outlive a page
# session: if it expires mid-session, an owner whose admin rights come only from
# Discord (no durable ``group_admins`` row) silently loses access to their
# group's settings/admin pages until they re-authenticate. Align the TTL with the
# session lifetime (re-login refreshes it). The ``group_admins`` table remains the
# primary, durable path and is unaffected by this cache.
_GUILDS_CACHE_TTL = int(
    os.getenv("WEB_API_GUILDS_CACHE_TTL")
    or os.getenv("WEB_API_SESSION_TTL")
    or str(7 * 24 * 3600)
)


def _rc():
    return getattr(redis_client, "client", None)


# --------------------------------------------------------------------------- #
# Session / identity
# --------------------------------------------------------------------------- #
def session_token() -> Optional[str]:
    return request.cookies.get(SESSION_COOKIE)


def require_claims() -> dict:
    """Return validated session claims, or abort 401 (RFC-7807)."""
    token = session_token()
    claims = verify_session(token) if token else None
    if not claims:
        # `code` extension members (here and in the assert_* helpers below,
        # web57a): a stable machine-readable reason the frontend can branch on
        # without string-matching the human-readable title/detail.
        abort_problem(
            401,
            "Not authenticated",
            "A valid session is required.",
            extra={"code": "auth_required"},
        )
    return claims


def current_user_id() -> int:
    """The authenticated user's id, or abort 401."""
    return int(require_claims()["sub"])


def optional_user_id() -> Optional[int]:
    """User id if a valid session is present, else None (no abort)."""
    token = session_token()
    claims = verify_session(token) if token else None
    return int(claims["sub"]) if claims else None


def render_token_authorized() -> bool:
    """True when the request carries the internal board-image render token
    (``X-Board-Image-Token`` == ``BOARD_IMAGE_TOKEN``).

    Lets the chrome-less ``/board-image/{id}`` render page (which the Discord bot
    screenshots) read ANY event — including private/draft — bypassing the viewer
    visibility gate, without a user session. Read-only; only the event-detail and
    board reads honor it. Disabled (always False) when the env token is unset."""
    import hmac

    expected = os.environ.get("BOARD_IMAGE_TOKEN", "")
    if not expected:
        return False
    provided = request.headers.get("X-Board-Image-Token", "")
    return bool(provided) and hmac.compare_digest(provided, expected)


# --------------------------------------------------------------------------- #
# Discord guild (MANAGE_GUILD) cache — populated at login (Task 02).
# --------------------------------------------------------------------------- #
def cache_manageable_guilds(user_id: int, guild_ids: Set[str]) -> None:
    conn = _rc()
    if conn is None:
        return
    try:
        conn.setex(
            f"{_GUILDS_CACHE_PREFIX}{user_id}",
            _GUILDS_CACHE_TTL,
            json.dumps(sorted(guild_ids)),
        )
    except Exception:
        pass


def manageable_guild_ids(user_id: int) -> Set[str]:
    conn = _rc()
    if conn is None:
        return set()
    try:
        raw = conn.get(f"{_GUILDS_CACHE_PREFIX}{user_id}")
        if not raw:
            return set()
        data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        return {str(g) for g in data}
    except Exception:
        return set()


def extract_manageable_guilds(discord_guilds: list) -> Set[str]:
    """From Discord's ``/users/@me/guilds`` payload, the guild ids the user can
    manage (owner or MANAGE_GUILD)."""
    return {g["id"] for g in extract_manageable_guild_meta(discord_guilds)}


def extract_manageable_guild_meta(discord_guilds: list) -> list:
    """From Discord's ``/users/@me/guilds`` payload, ``{id, name, icon}`` dicts
    for the guilds the user can manage (owner or MANAGE_GUILD)."""
    out: list = []
    seen: Set[str] = set()
    for g in discord_guilds or []:
        try:
            gid = str(g.get("id"))
            if gid in seen:
                continue
            manageable = bool(g.get("owner"))
            if not manageable:
                perms = int(g.get("permissions", 0))
                manageable = bool(perms & _MANAGE_GUILD)
            if manageable:
                seen.add(gid)
                out.append({
                    "id": gid,
                    "name": str(g.get("name") or ""),
                    "icon": g.get("icon") or None,
                })
        except Exception:
            continue
    return out


# Parallel cache holding {id, name, icon} for the same manageable guilds. The
# ids-only ``web:guilds:{uid}`` payload is parsed by role derivation on every
# request and must NOT change shape; names/icons live here instead (backs the
# group wizard's server picker via GET /me/guilds).
_GUILDMETA_CACHE_PREFIX = "web:guildmeta:"


def cache_manageable_guild_meta(user_id: int, guilds: list) -> None:
    conn = _rc()
    if conn is None:
        return
    try:
        conn.setex(
            f"{_GUILDMETA_CACHE_PREFIX}{user_id}",
            _GUILDS_CACHE_TTL,
            json.dumps(guilds),
        )
    except Exception:
        pass


def manageable_guild_meta(user_id: int) -> list:
    conn = _rc()
    if conn is None:
        return []
    try:
        raw = conn.get(f"{_GUILDMETA_CACHE_PREFIX}{user_id}")
        if not raw:
            return []
        data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# DB helpers (call inside a to_thread block with a Session)
# --------------------------------------------------------------------------- #
def load_user(s, user_id: int) -> Optional[User]:
    return s.query(User).filter(User.user_id == user_id).first()


def resolve_group_role(
    s,
    user_id: int,
    group_id: int,
    manage_guild_ids: Optional[Set[str]] = None,
    user: Optional[User] = None,
) -> Optional[str]:
    """Derive the user's role on a group (§7.2): 'owner' | 'admin' | 'member'
    | None (not a member and no admin rights).

    Superadmins resolve to 'owner' on every group unconditionally, even ones
    they've never joined — site staff can administer any group's settings the
    same way a real group owner would, from `/admin/groups`."""
    manage_guild_ids = manage_guild_ids or set()

    group = s.query(Group).filter(Group.group_id == group_id).first()
    if not group:
        return None

    if user is None:
        user = load_user(s, user_id)
    if is_superadmin(user):
        return "owner"

    # MANAGE_GUILD on the group's linked Discord guild => owner-level.
    if group.guild_id and str(group.guild_id) in manage_guild_ids:
        return "owner"

    # Explicit web grant.
    grant = (
        s.query(GroupAdmin)
        .filter(GroupAdmin.group_id == group_id, GroupAdmin.user_id == user_id)
        .first()
    )
    if grant:
        return grant.role if grant.role in ("owner", "admin") else "admin"

    # Plain membership (any of the user's players belongs to the group).
    if user:
        for g in user.groups:
            if g.group_id == group_id:
                return "member"
    return None


def is_group_admin_role(role: Optional[str]) -> bool:
    return role in ("owner", "admin")


# --------------------------------------------------------------------------- #
# Event Manager role (web64a) — group-scoped event-editing WITHOUT group admin.
# Kept deliberately separate from resolve_group_role / is_group_admin_role: an
# event manager must NEVER resolve to owner/admin (that would leak the whole
# group-admin surface). Only the event gates (assert_event_editor / the event
# route _is_event_admin/_assert_event_admin) consult these; assert_group_admin
# never does.
# --------------------------------------------------------------------------- #
def is_event_manager(s, user_id: int, group_id) -> bool:
    """True when the user holds a group_event_managers grant on ``group_id``."""
    if user_id is None or group_id is None:
        return False
    return (
        s.query(GroupEventManager)
        .filter(GroupEventManager.group_id == group_id,
                GroupEventManager.user_id == user_id)
        .first()
        is not None
    )


def event_manager_group_ids(s, user_id: int) -> Set[int]:
    """Every group id the user manages events for (drives the /me payload)."""
    if user_id is None:
        return set()
    return {
        int(gid)
        for (gid,) in s.query(GroupEventManager.group_id)
        .filter(GroupEventManager.user_id == user_id)
        .all()
    }


def assert_group_admin(
    s,
    user_id: int,
    group_id: int,
    manage_guild_ids: Optional[Set[str]] = None,
    user: Optional[User] = None,
) -> str:
    """Abort 403 unless the user is owner/admin of the group. Returns the role."""
    role = resolve_group_role(s, user_id, group_id, manage_guild_ids, user)
    if not is_group_admin_role(role):
        abort_problem(
            403,
            "Forbidden",
            "Admin rights on this group are required.",
            extra={"code": "group_admin_required"},
        )
    return role


def assert_group_member(
    s,
    user_id: int,
    group_id: int,
    manage_guild_ids: Optional[Set[str]] = None,
    user: Optional[User] = None,
) -> str:
    """Abort 403 unless the user holds ANY role on the group (member and up).

    Subscription-pool contributions are open to every group member, not just
    admins — any member may add their own payment leg toward the group's tier.
    """
    role = resolve_group_role(s, user_id, group_id, manage_guild_ids, user)
    if role is None:
        abort_problem(
            403,
            "Forbidden",
            "Group membership is required.",
            extra={"code": "group_member_required"},
        )
    return role


def is_superadmin(user: Optional[User]) -> bool:
    return bool(user and getattr(user, "is_superadmin", False))


def assert_superadmin(user: Optional[User]) -> None:
    if not is_superadmin(user):
        abort_problem(
            403,
            "Forbidden",
            "Site staff access is required.",
            extra={"code": "staff_required"},
        )


def is_moderator(user: Optional[User]) -> bool:
    """Site moderator (or superadmin — staff implies moderator)."""
    return is_superadmin(user) or bool(user and getattr(user, "is_moderator", False))


def assert_moderator(user: Optional[User]) -> None:
    if not is_moderator(user):
        abort_problem(
            403,
            "Forbidden",
            "Moderator access is required.",
            extra={"code": "moderator_required"},
        )


def assert_group_entitlement(
    s,
    user_id: int,
    group_id: int,
    entitlement_key: str,
    *,
    manage_guild_ids: Optional[Set[str]] = None,
    user: Optional[User] = None,
) -> None:
    """Abort 403 unless the group's subscription includes ``entitlement_key``.

    Superadmins bypass entitlement checks (same as ``canAdminGroup`` on the
    front-end). Requires admin rights on the group first.
    """
    from web_api.entitlements import resolve_group_entitlements
    from web_api.entitlements_registry import get_entitlement_field

    if get_entitlement_field(entitlement_key) is None:
        abort_problem(500, "Invalid entitlement", f"Unknown entitlement '{entitlement_key}'.")

    assert_group_admin(s, user_id, group_id, manage_guild_ids, user)

    if user is None:
        user = load_user(s, user_id)
    if is_superadmin(user):
        return

    entitlements = resolve_group_entitlements(s, group_id, user=user)
    if not entitlements.get(entitlement_key):
        label = get_entitlement_field(entitlement_key)["label"]
        abort_problem(
            403,
            "Subscription required",
            f"Your group's subscription does not include {label}. "
            "Upgrade on the Subscription tab.",
            extra={"code": "entitlement_required", "entitlement": entitlement_key},
        )


def assert_event_editor(
    s,
    user_id: int,
    group_id: int,
    *,
    manage_guild_ids: Optional[Set[str]] = None,
    user: Optional[User] = None,
) -> None:
    """The event-editing gate for a standard/group event (web64a): allow group
    owners/admins OR group event-managers, and — for BOTH — require the group's
    ``events`` entitlement.

    Unlike ``assert_group_entitlement`` (which hard-requires group admin), this
    decouples the role check from the entitlement: an event manager passes the
    role half without ever satisfying ``assert_group_admin``, but the group must
    still carry the ``events`` tier. Superadmins bypass everything.
    """
    from web_api.entitlements import resolve_group_entitlements

    if user is None:
        user = load_user(s, user_id)
    if is_superadmin(user):
        return

    role = resolve_group_role(s, user_id, group_id, manage_guild_ids, user=user)
    if not (is_group_admin_role(role) or is_event_manager(s, user_id, group_id)):
        abort_problem(
            403,
            "Forbidden",
            "You must administer this group or be one of its event managers.",
            extra={"code": "event_admin_required"},
        )

    entitlements = resolve_group_entitlements(s, group_id, user=user)
    if not entitlements.get("events"):
        # web65a: a tier without the events entitlement may still hold a
        # rate-limited grant (an enabled event-rate-limit rule with
        # max_events > 0) — those groups manage events normally; the
        # frequency cap binds at activation instead.
        from db.event_rate_limits import group_has_rate_limited_events

        if not group_has_rate_limited_events(s, group_id):
            abort_problem(
                403,
                "Subscription required",
                "Your group's subscription does not include Events. "
                "Upgrade on the Subscription tab.",
                extra={"code": "entitlement_required", "entitlement": "events"},
            )


# --------------------------------------------------------------------------- #
# Request body
# --------------------------------------------------------------------------- #
async def json_body(required: bool = True) -> dict:
    """Parse the request JSON body into a dict, aborting 400 on malformed input."""
    try:
        data = await request.get_json(silent=True)
    except Exception:
        data = None
    if data is None:
        if required:
            abort_problem(400, "Invalid request body", "A JSON object body is required.")
        return {}
    if not isinstance(data, dict):
        abort_problem(400, "Invalid request body", "Expected a JSON object.")
    return data
