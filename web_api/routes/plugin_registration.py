"""The website half of registering a group from RuneLite.

  GET  /api/v1/plugin-registrations/<id>           -> what is being registered
  POST /api/v1/plugin-registrations/<id>/complete  -> create it in this server

The plugin starts a registration (api/routes/plugin_admin.py) and opens the
Discord sign-in with the bot added. That sign-in lands the leader on the
website's /register-group/<id> page, signed in, with the server Discord says
the bot was added to; the page calls ``complete``. The leader must manage
that server, the same rule as the website's own group wizard (POST /groups).
See services/plugin_registration.py.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify

from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import current_user_id, is_superadmin, json_body, load_user, manageable_guild_ids

plugin_registration_bp = Blueprint("v1_plugin_registration", __name__)


def _rc():
    from utils.redis import redis_client

    return redis_client.client


@plugin_registration_bp.get("/plugin-registrations/<reg_id>")
async def registration_preview(reg_id: str):
    from services import plugin_registration as reg

    found = reg.preview(_rc(), reg_id)
    if found is None:
        abort_problem(404, "Not found", reg.EXPIRED, extra={"code": "expired"})
    return private_no_store(jsonify(found))


@plugin_registration_bp.post("/plugin-registrations/<reg_id>/complete")
async def registration_complete(reg_id: str):
    from services import plugin_registration as reg

    user_id = current_user_id()
    body = await json_body()
    guild_id = str(body.get("guild_id") or "").strip()
    if not guild_id.isdigit():
        abort_problem(422, "No server", "Discord didn't say which server the bot was added to. "
                      "Start again from RuneLite and pick your clan's server.", extra={"code": "no_guild"})

    def _who():
        with db_session() as s:
            user = load_user(s, user_id)
            if user is None:
                abort_problem(401, "Not authenticated", "User not found.")
            return str(user.discord_id or ""), user.username or "", is_superadmin(user)

    discord_id, username, staff = await asyncio.to_thread(_who)
    if not staff and guild_id not in manageable_guild_ids(user_id):
        abort_problem(403, "Forbidden", "You don't manage that Discord server. Pick a server where you "
                      "have Manage Server.", extra={"code": "not_manager"})
    try:
        result = await reg.complete(_rc(), reg_id, user_id=user_id, discord_id=discord_id,
                                    username=username, guild_id=guild_id)
    except reg.RegistrationError as e:
        abort_problem(409, "Not registered", e.message, extra={"code": "refused"})
    return private_no_store(jsonify(result))
