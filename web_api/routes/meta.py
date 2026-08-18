"""Public app metadata endpoints.

  GET /api/v1/meta/bot-invite -> Discord bot invite info for the group wizard

No auth: this is static, public information (the client id is visible in every
bot interaction anyway).
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

from quart import Blueprint, jsonify

from web_api.common import with_cache_headers

meta_bp = Blueprint("v1_meta", __name__)

# The primary bot application id — same env + fallback as
# services.activity_launch_core.ACTIVITY_APP_ID.
_DEFAULT_CLIENT_ID = "1172933457010245762"


@meta_bp.get("/meta/bot-invite")
async def bot_invite():
    client_id = (os.getenv("DISCORD_BOT_CLIENT_ID") or "").strip() or _DEFAULT_CLIENT_ID
    # Default = services.guild_permissions.INVITE_PERMISSIONS (kept as a
    # literal here because web_api must not import the interactions lib; a
    # unit test pins the two equal). Was previously ABSENT — the invite asked
    # for no permissions while components.py asked for Administrator.
    permissions = (os.getenv("DISCORD_BOT_INVITE_PERMISSIONS") or "").strip() or "268553232"

    params = {"client_id": client_id, "scope": "bot applications.commands"}
    if permissions:
        params["permissions"] = permissions
    invite_url = f"https://discord.com/oauth2/authorize?{urlencode(params)}"

    resp = jsonify({
        "client_id": client_id,
        "permissions": permissions,
        "invite_url": invite_url,
    })
    return with_cache_headers(resp, max_age=3600)
