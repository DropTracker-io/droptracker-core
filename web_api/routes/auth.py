"""Task 02 — Discord OAuth session issuance.

The BFF performs the Discord redirect + code exchange itself and then calls us:

    POST /api/v1/auth/discord
    { "discord_profile": {id, username, global_name, avatar},
      "discord_access_token": "..." }
    -> { "session_token": "<JWT>" }

We find-or-create the ``users`` row keyed on ``discord_id``, cache the user's
display profile + manageable guilds (for role derivation), and mint a stateless
JWT session (``web_api/session.py``).

POST /api/v1/auth/logout revokes the presented token (best-effort).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time

import httpx
from quart import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError

from db import User
from utils.redis import redis_client
from web_api.common import abort_problem, db_session
from web_api.deps import (
    SESSION_COOKIE,
    cache_manageable_guild_meta,
    cache_manageable_guilds,
    extract_manageable_guild_meta,
    session_token,
)
from web_api.session import mint_session, revoke_session

auth_bp = Blueprint("v1_auth", __name__)

_SNOWFLAKE = re.compile(r"^\d{5,25}$")
_PROFILE_PREFIX = "web:profile:"
_PROFILE_TTL = 30 * 24 * 3600  # 30 days
_DISCORD_GUILDS_URL = "https://discord.com/api/users/@me/guilds"

# Bootstrap superadmins by Discord id (comma-separated env). Any listed user is
# granted is_superadmin on login. This is the simplest way to designate site
# staff without shell access; `scripts/set_superadmin.py` is the manual path.
_SUPERADMIN_DISCORD_IDS = {
    x.strip()
    for x in os.getenv("WEB_SUPERADMIN_DISCORD_IDS", "").split(",")
    if x.strip()
}


def _rc():
    return getattr(redis_client, "client", None)


def _avatar_url(discord_id: str, avatar: str | None) -> str | None:
    if avatar:
        ext = "gif" if str(avatar).startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.{ext}"
    return None


def cache_profile(user_id: int, discord_id: str, display_name: str, avatar: str | None) -> None:
    conn = _rc()
    if conn is None:
        return
    try:
        conn.setex(
            f"{_PROFILE_PREFIX}{user_id}",
            _PROFILE_TTL,
            json.dumps(
                {
                    "discord_id": str(discord_id),
                    "display_name": display_name,
                    "avatar_url": _avatar_url(str(discord_id), avatar),
                }
            ),
        )
    except Exception:
        pass


def get_cached_profile(user_id: int) -> dict:
    conn = _rc()
    if conn is None:
        return {}
    try:
        raw = conn.get(f"{_PROFILE_PREFIX}{user_id}")
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return {}


def _find_or_create_user(discord_id: str, display_name: str) -> int:
    """Find-or-create the ``users`` row keyed on ``discord_id``; return user_id.

    DB-only (no Discord bot side-effects), mirroring the DB portion of
    ``try_create_user`` / ``db.group_creation._ensure_user``.
    """
    is_bootstrap_admin = str(discord_id) in _SUPERADMIN_DISCORD_IDS
    with db_session() as s:
        user = s.query(User).filter(User.discord_id == str(discord_id)).first()
        if user:
            # Grant (never auto-revoke) superadmin for bootstrap-listed ids.
            if is_bootstrap_admin and not getattr(user, "is_superadmin", False):
                user.is_superadmin = True
                s.commit()
            return user.user_id

        username = (display_name or "")[:20] or None
        user = User(
            auth_token=secrets.token_hex(8),  # 16 chars, matches column width
            discord_id=str(discord_id),
            username=username,
            is_superadmin=is_bootstrap_admin,
        )
        s.add(user)
        try:
            s.commit()
        except IntegrityError:
            # Lost an insert race — users.discord_id is unique, so a
            # concurrent login/bot path created the row first. Use theirs.
            s.rollback()
            user = s.query(User).filter(User.discord_id == str(discord_id)).first()
        return user.user_id


async def _fetch_manageable_guilds(access_token: str) -> list:
    """Best-effort fetch of the guilds the user can manage (never fails login).

    Returns ``{id, name, icon}`` dicts; callers derive the ids-only set from it.
    """
    if not access_token:
        return []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _DISCORD_GUILDS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code == 200:
            return extract_manageable_guild_meta(resp.json())
    except Exception:
        pass
    return []


@auth_bp.post("/auth/discord")
async def auth_discord():
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        abort_problem(400, "Invalid request body", "Expected a JSON object.")

    profile = body.get("discord_profile") or {}
    access_token = body.get("discord_access_token") or ""
    discord_id = str(profile.get("id") or "")

    if not _SNOWFLAKE.match(discord_id):
        abort_problem(400, "Invalid Discord profile", "A valid Discord user id is required.")

    display_name = (
        profile.get("global_name")
        or profile.get("username")
        or f"User {discord_id}"
    )
    avatar = profile.get("avatar")

    user_id = await asyncio.to_thread(_find_or_create_user, discord_id, display_name)

    # Cache display profile + manageable guilds for /me and role derivation.
    cache_profile(user_id, discord_id, display_name, avatar)
    guild_meta = await _fetch_manageable_guilds(access_token)
    if guild_meta:
        # web:guilds:{uid} (ids only) feeds role derivation — shape frozen.
        cache_manageable_guilds(user_id, {g["id"] for g in guild_meta})
        # web:guildmeta:{uid} adds names/icons for the wizard's server picker.
        cache_manageable_guild_meta(user_id, guild_meta)

    token = mint_session(user_id)
    return jsonify({"session_token": token})


@auth_bp.post("/auth/logout")
async def auth_logout():
    token = session_token()
    if token:
        revoke_session(token)
    return jsonify({"ok": True})
