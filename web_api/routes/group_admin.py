"""Task 10 — group management + creation wizard.

Members / hidden players / WOM sync / diagnostics (session + group admin):
  GET   /api/v1/groups/{id}/members
  GET   /api/v1/groups/{id}/hidden-players
  PATCH /api/v1/groups/{id}/hidden-players     { player_id, hidden }
  POST  /api/v1/groups/{id}/wom-sync
  GET   /api/v1/groups/{id}/diagnostics

Creation wizard (session):
  GET   /api/v1/groups/wom-lookup/{womId}
  GET   /api/v1/groups/guild-status/{guildId}
  POST  /api/v1/groups                          { name, wom_id, guild_id, discord_url }
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from quart import Blueprint, jsonify, request

from db import Group, GroupAdmin, GroupConfiguration, Guild, IgnoredPlayer, Player, User
from db.models import NotifiedSubmission
from utils.redis import redis_client
from web_api.common import (
    abort_problem,
    db_session,
    money,
    parse_page,
    period_to_partition,
    player_month_total,
    private_no_store,
)
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

group_admin_bp = Blueprint("v1_group_admin", __name__)


def _rc():
    return getattr(redis_client, "client", None)


# --------------------------------------------------------------------------- #
# Members + hidden players
# --------------------------------------------------------------------------- #
@group_admin_bp.get("/groups/<int:group_id>/members")
async def group_members(group_id: int):
    user_id = current_user_id()
    page, limit = parse_page(request)
    q = (request.args.get("q") or "").strip().lower()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                abort_problem(404, "Group not found", f"No group with id {group_id}.")

            hidden_ids = {
                pid for (pid,) in s.query(IgnoredPlayer.player_id)
                .filter(IgnoredPlayer.group_id == group_id).all()
            }
            partition = period_to_partition("all")
            rows = (
                s.query(Player.player_id, Player.player_name)
                .join(Player.groups)
                .filter(Group.group_id == group_id)
                .all()
            )
            members = [
                {
                    "id": pid,
                    "name": name,
                    "total_loot": money(player_month_total(pid, partition)),
                    "hidden": pid in hidden_ids,
                }
                for pid, name in rows
                if not q or q in (name or "").lower()
            ]
            members.sort(key=lambda m: m["total_loot"]["value"], reverse=True)
            total = len(members)
            start = (page - 1) * limit
            return {
                "members": members[start:start + limit],
                "meta": {"page": page, "limit": limit, "total": total},
            }

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@group_admin_bp.get("/groups/<int:group_id>/hidden-players")
async def get_hidden_players(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            ids = [pid for (pid,) in s.query(IgnoredPlayer.player_id)
                   .filter(IgnoredPlayer.group_id == group_id).all()]
            return {"hidden_player_ids": ids}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@group_admin_bp.patch("/groups/<int:group_id>/hidden-players")
async def set_hidden_player(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    player_id = body.get("player_id")
    hidden = body.get("hidden")
    if not isinstance(player_id, int) or not isinstance(hidden, bool):
        abort_problem(422, "Invalid body", "'player_id' (int) and 'hidden' (bool) required.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            existing = (
                s.query(IgnoredPlayer)
                .filter(IgnoredPlayer.group_id == group_id, IgnoredPlayer.player_id == player_id)
                .first()
            )
            if hidden and not existing:
                s.add(IgnoredPlayer(group_id=group_id, player_id=player_id))
            elif not hidden and existing:
                s.delete(existing)
            s.commit()

    await asyncio.to_thread(_apply)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# WOM sync
# --------------------------------------------------------------------------- #
@group_admin_bp.post("/groups/<int:group_id>/wom-sync")
async def wom_sync(group_id: int):
    user_id = current_user_id()

    def _authorize():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                abort_problem(404, "Group not found", f"No group with id {group_id}.")
            if not group.wom_id:
                abort_problem(400, "No WOM id", "This group has no WOM id configured.")
            return int(group.wom_id)

    wom_id = await asyncio.to_thread(_authorize)

    try:
        from db.ops import sync_group_from_wom_with_stats

        result = await sync_group_from_wom_with_stats(wom_id=wom_id)
    except Exception as e:
        abort_problem(502, "WOM sync failed", str(e))

    if result.get("on_cooldown"):
        # Return last-known counts rather than blocking/erroring (§15).
        return jsonify({
            "added": 0,
            "removed": 0,
            "total": int(result.get("total_members") or 0),
            "synced_ts": int(time.time()),
        })
    return jsonify({
        "added": int(result.get("added") or 0),
        "removed": int(result.get("removed") or 0),
        "total": int(result.get("total_members") or 0),
        "synced_ts": int(time.time()),
    })


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
@group_admin_bp.get("/groups/<int:group_id>/diagnostics")
async def diagnostics(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                abort_problem(404, "Group not found", f"No group with id {group_id}.")

            # Intake heartbeat: any drop in the last 24h.
            last_drop = s.execute(text("SELECT MAX(date_added) FROM drops")).scalar()
            intake_healthy = bool(last_drop and (datetime.now() - last_drop) < timedelta(hours=24))

            last_sub = (
                s.query(NotifiedSubmission.date_added)
                .filter(NotifiedSubmission.group_id == group_id)
                .order_by(NotifiedSubmission.date_added.desc())
                .first()
            )
            last_submission_ts = int(last_sub[0].timestamp()) if last_sub and last_sub[0] else None

            # Per-day submission counts for the last 7 days.
            activity = []
            for i in range(6, -1, -1):
                day = (datetime.now() - timedelta(days=i)).date()
                start = datetime(day.year, day.month, day.day)
                end = start + timedelta(days=1)
                count = (
                    s.query(NotifiedSubmission)
                    .filter(
                        NotifiedSubmission.group_id == group_id,
                        NotifiedSubmission.date_added >= start,
                        NotifiedSubmission.date_added < end,
                    )
                    .count()
                )
                activity.append({"date": day.isoformat(), "submissions": int(count)})

            warnings = []
            drop_channel = (
                s.query(GroupConfiguration)
                .filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == "channel_id_to_post_loot",
                )
                .first()
            )
            if not drop_channel or not drop_channel.config_value:
                warnings.append("No drops channel (channel_id_to_post_loot) configured — drop notifications won't post.")
            if not group.guild_id:
                warnings.append("No Discord guild linked to this group.")

            # members_synced_ts from the WOM sync cooldown cache, if present.
            members_synced_ts = None
            conn = _rc()
            if conn is not None:
                try:
                    raw = conn.get(f"wom_sync_last:{group.wom_id}")
                    if raw:
                        members_synced_ts = int(float(raw))
                except Exception:
                    members_synced_ts = None

            return {
                "intake_healthy": intake_healthy,
                "last_submission_ts": last_submission_ts,
                "members_synced_ts": members_synced_ts,
                "activity_7d": activity,
                "warnings": warnings,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


# --------------------------------------------------------------------------- #
# Creation wizard
# --------------------------------------------------------------------------- #
@group_admin_bp.get("/groups/wom-lookup/<int:wom_id>")
async def wom_lookup(wom_id: int):
    current_user_id()  # session required

    def _already():
        with db_session() as s:
            g = s.query(Group).filter(Group.wom_id == int(wom_id)).first()
            return g is not None

    already = await asyncio.to_thread(_already)
    try:
        from utils.wiseoldman import check_group_by_id

        group_name, member_count, _members = await check_group_by_id(int(wom_id))
    except Exception as e:
        abort_problem(502, "WOM lookup failed", str(e))

    if not group_name:
        abort_problem(404, "WOM group not found", f"No WiseOldMan group with id {wom_id}.")

    return jsonify({
        "wom_id": int(wom_id),
        "name": group_name,
        "member_count": int(member_count or 0),
        "already_registered": bool(already),
    })


async def _bot_in_guild(guild_id: str):
    """Best-effort check whether the bot is in a guild (cached in Redis)."""
    cache_key = f"bot_in_guild:{guild_id}"
    conn = _rc()
    if conn is not None:
        try:
            cached = conn.get(cache_key)
            if cached is not None:
                val = cached.decode() if isinstance(cached, (bytes, bytearray)) else cached
                return val == "1"
        except Exception:
            pass
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"https://discord.com/api/v10/guilds/{guild_id}",
                headers={"Authorization": f"Bot {token}"},
            )
        present = resp.status_code == 200
    except Exception:
        return None
    if conn is not None:
        try:
            conn.setex(cache_key, 300, "1" if present else "0")
        except Exception:
            pass
    return present


@group_admin_bp.get("/groups/guild-status/<guild_id>")
async def guild_status(guild_id: str):
    user_id = current_user_id()
    guild_id = str(guild_id)

    def _load():
        with db_session() as s:
            guild = s.query(Guild).filter(Guild.guild_id == guild_id).first()
            owns_group = False
            gid = None
            if guild and guild.group_id is not None:
                group = s.query(Group).filter(Group.group_id == guild.group_id).first()
                if group:
                    owns_group = True
                    gid = group.group_id
            return guild is not None, owns_group, gid

    have_record, owns_group, gid = await asyncio.to_thread(_load)
    bot_present = have_record
    if not bot_present:
        verified = await _bot_in_guild(guild_id)
        if verified is not None:
            bot_present = verified

    return jsonify({
        "guild_id": guild_id,
        "bot_present": bool(bot_present),
        "owns_group": bool(owns_group),
        "group_id": gid,
    })


@group_admin_bp.post("/groups")
async def create_group():
    user_id = current_user_id()
    body = await json_body()

    name = (body.get("name") or "").strip()
    wom_id = body.get("wom_id")
    guild_id = str(body.get("guild_id") or "").strip()
    discord_url = (body.get("discord_url") or "").strip()

    if not (1 <= len(name) <= 100):
        abort_problem(422, "Invalid name", "Group name must be 1–100 characters.")
    if not isinstance(wom_id, int) or wom_id <= 0:
        abort_problem(422, "Invalid wom_id", "'wom_id' must be a positive integer.")
    if not guild_id:
        abort_problem(422, "Invalid guild_id", "'guild_id' is required.")

    # The caller must manage the target guild (MANAGE_GUILD), from login cache.
    manage_ids = manageable_guild_ids(user_id)
    if manage_ids and guild_id not in manage_ids:
        abort_problem(403, "Forbidden", "You do not manage the selected Discord server.")

    def _owner_identity():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                abort_problem(401, "Not authenticated", "User not found.")
            return str(user.discord_id or ""), user.username

    owner_discord_id, owner_username = await asyncio.to_thread(_owner_identity)

    try:
        from db.group_creation import create_web_group

        result = await create_web_group(
            group_name=name,
            wom_id=wom_id,
            guild_id=guild_id,
            owner_discord_id=owner_discord_id,
            owner_username=owner_username,
        )
    except Exception as e:
        abort_problem(500, "Group creation failed", str(e))

    if not result.get("success"):
        status = result.get("status")
        if status in ("wom_conflict", "guild_conflict", "already_registered"):
            abort_problem(409, "Group already exists", result.get("message"))
        if status == "invalid_wom":
            abort_problem(422, "Invalid WOM id", result.get("message"))
        abort_problem(500, "Group creation failed", result.get("message"))

    new_group_id = result.get("group_id")

    # Seed the caller as owner + persist discord_url (best-effort).
    def _post_create():
        with db_session() as s:
            existing = (
                s.query(GroupAdmin)
                .filter(GroupAdmin.group_id == new_group_id, GroupAdmin.user_id == user_id)
                .first()
            )
            if not existing:
                s.add(GroupAdmin(group_id=new_group_id, user_id=user_id, role="owner"))
            if discord_url:
                row = (
                    s.query(GroupConfiguration)
                    .filter(
                        GroupConfiguration.group_id == new_group_id,
                        GroupConfiguration.config_key == "discord_url",
                    )
                    .first()
                )
                if row:
                    row.config_value = discord_url
                else:
                    s.add(GroupConfiguration(
                        group_id=new_group_id, config_key="discord_url", config_value=discord_url
                    ))
            s.commit()

    await asyncio.to_thread(_post_create)
    return jsonify({"id": new_group_id})
