"""
Web-based group creation API.

These endpoints back the website's "Create a Group" wizard. They allow the
XenForo front-end to register a new DropTracker group without the user having to
run the Discord ``/create-group`` slash command.

Authentication:
    All endpoints here are server-to-server only (called by the website's PHP
    backend, never directly by a browser). They are protected by a shared secret
    (the ``XF_KEY`` environment variable) supplied either as an
    ``Authorization: Bearer <key>`` header or an ``X-API-Key`` header.

Parity:
    The actual creation logic lives in :func:`db.group_creation.create_web_group`,
    which is a 1:1 replica of the Discord bot's ``/create-group`` command. The
    Discord command itself is unchanged.
"""

import asyncio
import os
from datetime import timedelta
from functools import wraps

from quart import Blueprint, current_app, jsonify, request
from quart_cors import route_cors
from quart_rate_limiter import rate_limit

from api.core import get_db_session, redis_client
from db.group_creation import create_web_group
from db.models import Group, Guild

group_create_bp = Blueprint("group_create", __name__)

# How long (seconds) to cache an authoritative bot-presence result per guild.
_BOT_PRESENCE_CACHE_TTL = 300

# Status -> HTTP code mapping for the create endpoint.
_STATUS_HTTP = {
    "created": 201,
    "already_registered": 409,
    "guild_conflict": 409,
    "wom_conflict": 409,
    "invalid_wom": 400,
    "db_error": 500,
}


def _service_secret() -> str:
    """Return the configured server-to-server shared secret (``XF_KEY``)."""
    return (os.getenv("XF_KEY") or "").strip()


def _extract_request_key() -> str:
    """Pull the caller-supplied service key from the request headers."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return (request.headers.get("X-API-Key", "") or "").strip()


async def _run_initial_wom_sync(wom_id: int):
    """Background task: perform the first WOM membership sync for a new group.

    Runs after the create response has been returned so the wizard stays snappy;
    by the time the owner views their lootboard it will be populated. Best-effort
    only - failures are logged and swallowed.
    """
    try:
        from db.ops import sync_group_from_wom_with_stats

        result = await sync_group_from_wom_with_stats(wom_id=int(wom_id))
        if result.get("on_cooldown"):
            print(f"[group_create] Initial WOM sync skipped (cooldown) for wom_id={wom_id}")
        else:
            print(
                f"[group_create] Initial WOM sync done for wom_id={wom_id}: "
                f"+{len(result.get('added', []))} members"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[group_create] Initial WOM sync failed for wom_id={wom_id}: {exc}")


async def _bot_in_guild(guild_id: str):
    """Authoritatively check (and cache) whether the bot is in a guild.

    Returns True/False when determinable, or None if it can't be checked (e.g.
    no bot token configured or Discord is unreachable). Results are cached in
    Redis to avoid hammering the Discord API.
    """
    cache_key = f"bot_in_guild:{guild_id}"
    try:
        cached = redis_client.get(cache_key)
        if cached is not None:
            return cached == "1"
    except Exception:
        pass

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        return None

    try:
        import aiohttp

        url = f"https://discord.com/api/v10/guilds/{guild_id}"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as sess:
            async with sess.get(
                url, headers={"Authorization": f"Bot {token}"}
            ) as resp:
                present = resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"[group_create] Bot presence check failed for {guild_id}: {exc}")
        return None

    try:
        redis_client.setex(cache_key, _BOT_PRESENCE_CACHE_TTL, "1" if present else "0")
    except Exception:
        pass
    return present


def require_service_key(func):
    """Decorator enforcing the shared ``XF_KEY`` secret on a route."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        expected = _service_secret()
        if not expected:
            # Fail closed: if no secret is configured the endpoint is disabled.
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Group creation API is not configured on the server.",
                    }
                ),
                503,
            )
        provided = _extract_request_key()
        if not provided or provided != expected:
            return jsonify({"success": False, "error": "Unauthorized."}), 401
        return await func(*args, **kwargs)

    return wrapper


@group_create_bp.post("/groups/create")
@route_cors(allow_origin="https://www.droptracker.io")
@rate_limit(limit=20, period=timedelta(seconds=60))
@require_service_key
async def create_group():
    """Create a new DropTracker group on behalf of a website user.

    Expected JSON body:
        {
            "group_name": str,
            "wom_id": int | str,
            "guild_id": str,            # Discord server id
            "owner_discord_id": str,    # creator's Discord user id
            "owner_username": str|null  # optional
        }
    """
    body = await request.get_json(silent=True) or {}

    group_name = (body.get("group_name") or "").strip()
    wom_id = body.get("wom_id")
    guild_id = body.get("guild_id")
    owner_discord_id = body.get("owner_discord_id")
    owner_username = body.get("owner_username")
    # Defaults to True: kick off an initial WOM sync so the new group's
    # lootboard populates without waiting for the scheduled sync.
    initial_sync = body.get("initial_sync", True)

    # --- Basic validation ---
    missing = [
        field
        for field, value in (
            ("group_name", group_name),
            ("wom_id", wom_id),
            ("guild_id", guild_id),
            ("owner_discord_id", owner_discord_id),
        )
        if value in (None, "")
    ]
    if missing:
        return (
            jsonify(
                {
                    "success": False,
                    "status": "invalid_request",
                    "error": f"Missing required field(s): {', '.join(missing)}",
                }
            ),
            400,
        )

    if len(group_name) > 30:
        return (
            jsonify(
                {
                    "success": False,
                    "status": "invalid_request",
                    "error": "Group name must be 30 characters or fewer.",
                }
            ),
            400,
        )

    try:
        result = await create_web_group(
            group_name=group_name,
            wom_id=wom_id,
            guild_id=guild_id,
            owner_discord_id=owner_discord_id,
            owner_username=owner_username,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[group_create] Unexpected error: {exc}")
        return (
            jsonify(
                {
                    "success": False,
                    "status": "db_error",
                    "error": "An unexpected error occurred while creating the group.",
                }
            ),
            500,
        )

    # On success, kick off a non-blocking initial WOM membership sync so the
    # group's lootboard is populated shortly after creation.
    if result.get("success") and result.get("status") == "created" and initial_sync:
        synced_wom_id = result.get("wom_id")
        if synced_wom_id:
            try:
                current_app.add_background_task(_run_initial_wom_sync, int(synced_wom_id))
            except Exception as exc:  # noqa: BLE001
                # Fall back to a detached task if the app helper is unavailable.
                print(f"[group_create] Could not schedule initial sync: {exc}")
                asyncio.ensure_future(_run_initial_wom_sync(int(synced_wom_id)))

    http_code = _STATUS_HTTP.get(result.get("status", ""), 200 if result.get("success") else 400)
    return jsonify(result), http_code


@group_create_bp.get("/groups/guild-status/<guild_id>")
@route_cors(allow_origin="https://www.droptracker.io")
@rate_limit(limit=60, period=timedelta(seconds=60))
@require_service_key
async def guild_status(guild_id: str):
    """Report whether a Discord guild already owns a group / has the bot.

    Used by the wizard's server-selection step to decide whether to show
    "Invite the bot", "Create group", or "Configure existing group".

    Response:
        {
            "success": true,
            "guild_id": str,
            "bot_present": bool,     # the bot has a record of this guild
            "has_group": bool,       # the guild already owns a DropTracker group
            "group_id": int|null,
            "group_name": str|null,
            "wom_id": int|null
        }
    """
    guild_id = str(guild_id)
    db_session = get_db_session()
    try:
        guild = db_session.query(Guild).filter(Guild.guild_id == guild_id).first()
        bot_present = guild is not None
        # If we have no local record, confirm with Discord directly so the
        # wizard doesn't dead-end on servers the bot is already in.
        if not bot_present:
            verified = await _bot_in_guild(guild_id)
            if verified is not None:
                bot_present = verified
        payload = {
            "success": True,
            "guild_id": guild_id,
            "bot_present": bot_present,
            "has_group": False,
            "group_id": None,
            "group_name": None,
            "wom_id": None,
        }
        if guild and guild.group_id is not None:
            group = (
                db_session.query(Group).filter(Group.group_id == guild.group_id).first()
            )
            if group:
                payload.update(
                    has_group=True,
                    group_id=group.group_id,
                    group_name=group.group_name,
                    wom_id=group.wom_id,
                )
        return jsonify(payload), 200
    finally:
        db_session.close()


@group_create_bp.get("/groups/wom-lookup/<int:wom_id>")
@route_cors(allow_origin="https://www.droptracker.io")
@rate_limit(limit=30, period=timedelta(seconds=60))
@require_service_key
async def wom_lookup(wom_id: int):
    """Look up a WiseOldMan group by id for the wizard.

    Returns the WOM group's name and member count, and whether that WOM id is
    already registered with the DropTracker (so the wizard can warn the user
    before they submit).
    """
    # Imported lazily so this lightweight module doesn't pull the WOM client
    # stack unless the endpoint is actually used.
    from utils.wiseoldman import check_group_by_id

    db_session = get_db_session()
    try:
        already = (
            db_session.query(Group).filter(Group.wom_id == int(wom_id)).first()
        )
        already_group_id = already.group_id if already else None
    finally:
        db_session.close()

    try:
        group_name, member_count, _members = await check_group_by_id(int(wom_id))
    except Exception as exc:  # noqa: BLE001
        print(f"[group_create] WOM lookup failed for {wom_id}: {exc}")
        return (
            jsonify({"success": False, "error": "Failed to reach WiseOldMan."}),
            502,
        )

    if not group_name:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"No WiseOldMan group found with id {wom_id}.",
                }
            ),
            404,
        )

    return (
        jsonify(
            {
                "success": True,
                "wom_id": int(wom_id),
                "group_name": group_name,
                "member_count": member_count,
                "already_registered": already_group_id is not None,
                "existing_group_id": already_group_id,
            }
        ),
        200,
    )
