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

from db import Group, GroupAdmin, User
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
        abort_problem(401, "Not authenticated", "A valid session is required.")
    return claims


def current_user_id() -> int:
    """The authenticated user's id, or abort 401."""
    return int(require_claims()["sub"])


def optional_user_id() -> Optional[int]:
    """User id if a valid session is present, else None (no abort)."""
    token = session_token()
    claims = verify_session(token) if token else None
    return int(claims["sub"]) if claims else None


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
    out: Set[str] = set()
    for g in discord_guilds or []:
        try:
            gid = str(g.get("id"))
            if g.get("owner"):
                out.add(gid)
                continue
            perms = int(g.get("permissions", 0))
            if perms & _MANAGE_GUILD:
                out.add(gid)
        except Exception:
            continue
    return out


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
        abort_problem(403, "Forbidden", "Admin rights on this group are required.")
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
        abort_problem(403, "Forbidden", "Group membership is required.")
    return role


def is_superadmin(user: Optional[User]) -> bool:
    return bool(user and getattr(user, "is_superadmin", False))


def assert_superadmin(user: Optional[User]) -> None:
    if not is_superadmin(user):
        abort_problem(403, "Forbidden", "Site staff access is required.")


def is_moderator(user: Optional[User]) -> bool:
    """Site moderator (or superadmin — staff implies moderator)."""
    return is_superadmin(user) or bool(user and getattr(user, "is_moderator", False))


def assert_moderator(user: Optional[User]) -> None:
    if not is_moderator(user):
        abort_problem(403, "Forbidden", "Moderator access is required.")


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
