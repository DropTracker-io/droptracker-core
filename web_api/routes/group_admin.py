"""Task 10 — group management + creation wizard.

Members / hidden players / WOM sync / diagnostics (session + group admin):
  GET   /api/v1/groups/{id}/members
  GET   /api/v1/groups/{id}/hidden-players
  PATCH /api/v1/groups/{id}/hidden-players     { player_id, hidden }
  POST  /api/v1/groups/{id}/wom-sync
  GET   /api/v1/groups/{id}/diagnostics

Authorized users (post-creation admin management, XF parity):
  GET    /api/v1/groups/{id}/authorized-users
  POST   /api/v1/groups/{id}/authorized-users   { identifier }
  DELETE /api/v1/groups/{id}/authorized-users   { user_id | discord_id }

Creation wizard (session):
  GET   /api/v1/groups/wom-lookup/{womId}
  GET   /api/v1/groups/guild-status/{guildId}
  POST  /api/v1/groups                          { name, wom_id, guild_id, discord_url }
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta

from sqlalchemy import func, text

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
# Authorized users — who may administer the group besides the creator.
#
# Two stores are kept in sync (XF-era parity):
#   * ``group_admins`` rows            → drive website roles (deps.resolve_group_role)
#   * ``authed_users`` group config    → JSON list of Discord IDs; drives the
#     Discord bot's slash-command authorization (commands/utils.py).
# --------------------------------------------------------------------------- #
_AUTHED_USERS_KEY = "authed_users"


def _load_authed_ids(s, group_id: int):
    """Return (config_row_or_None, list[str] of Discord IDs)."""
    row = (
        s.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == _AUTHED_USERS_KEY,
        )
        .first()
    )
    raw = None
    if row is not None:
        raw = row.config_value or getattr(row, "long_value", None)
    if not raw:
        return row, []
    try:
        data = json.loads(raw)
    except Exception:
        return row, []
    if not isinstance(data, list):
        return row, []
    return row, [str(v) for v in data if v]


def _store_authed_ids(s, group_id: int, row, ids: list[str]) -> None:
    """Persist the Discord-ID list; short lists stay in config_value so the
    bot's existing readers keep working, oversized lists spill to long_value."""
    payload = json.dumps(ids)
    if row is None:
        row = GroupConfiguration(group_id=group_id, config_key=_AUTHED_USERS_KEY)
        s.add(row)
    if len(payload) <= 255:
        row.config_value = payload
        row.long_value = None
    else:
        row.config_value = ""
        row.long_value = payload


def _authorized_entries(s, group_id: int) -> list[dict]:
    """Merged view of both stores, one entry per person."""
    grants = s.query(GroupAdmin).filter(GroupAdmin.group_id == group_id).all()
    _, authed_ids = _load_authed_ids(s, group_id)

    entries: dict[str, dict] = {}  # keyed by discord_id or "user:{id}"

    grant_users = {
        u.user_id: u
        for u in s.query(User).filter(User.user_id.in_([g.user_id for g in grants])).all()
    } if grants else {}
    for g in grants:
        u = grant_users.get(g.user_id)
        discord_id = str(u.discord_id) if (u and u.discord_id) else None
        key = discord_id or f"user:{g.user_id}"
        entries[key] = {
            "user_id": g.user_id,
            "discord_id": discord_id,
            "username": (u.username if u else None) or None,
            "role": g.role if g.role in ("owner", "admin") else "admin",
            "sources": ["web"],
        }

    if authed_ids:
        id_users = {
            str(u.discord_id): u
            for u in s.query(User).filter(User.discord_id.in_(authed_ids)).all()
        }
        for did in authed_ids:
            if did in entries:
                entries[did]["sources"].append("discord")
                continue
            u = id_users.get(did)
            entries[did] = {
                "user_id": u.user_id if u else None,
                "discord_id": did,
                "username": (u.username if u else None) or None,
                "role": "admin",
                "sources": ["discord"],
            }

    out = list(entries.values())
    out.sort(key=lambda e: (e["role"] != "owner", (e["username"] or "￿").lower()))
    return out


@group_admin_bp.get("/groups/<int:group_id>/authorized-users")
async def list_authorized_users(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            return {"users": _authorized_entries(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@group_admin_bp.post("/groups/<int:group_id>/authorized-users")
async def add_authorized_user(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    identifier = str(body.get("identifier") or "").strip()
    if not identifier:
        abort_problem(422, "Missing identifier", "Provide a Discord ID or DropTracker username.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            target_user = None
            discord_id = None
            if identifier.isdigit() and 15 <= len(identifier) <= 20:
                discord_id = identifier
                target_user = s.query(User).filter(User.discord_id == identifier).first()
            else:
                target_user = (
                    s.query(User)
                    .filter(func.lower(User.username) == identifier.lower())
                    .first()
                )
                if target_user is None:
                    abort_problem(
                        404,
                        "User not found",
                        f"No DropTracker user named '{identifier}'. "
                        "You can also add someone by their Discord ID.",
                    )
                discord_id = str(target_user.discord_id) if target_user.discord_id else None

            changed = False
            if discord_id:
                row, ids = _load_authed_ids(s, group_id)
                if discord_id not in ids:
                    ids.append(discord_id)
                    _store_authed_ids(s, group_id, row, ids)
                    changed = True
            if target_user is not None:
                existing = (
                    s.query(GroupAdmin)
                    .filter(
                        GroupAdmin.group_id == group_id,
                        GroupAdmin.user_id == target_user.user_id,
                    )
                    .first()
                )
                if existing is None:
                    s.add(GroupAdmin(
                        group_id=group_id,
                        user_id=target_user.user_id,
                        role="admin",
                        granted_by=user_id,
                    ))
                    changed = True

            if not changed:
                abort_problem(409, "Already authorized", "That user is already authorized.")
            s.commit()
            return {"users": _authorized_entries(s, group_id)}

    return jsonify(await asyncio.to_thread(_apply))


@group_admin_bp.delete("/groups/<int:group_id>/authorized-users")
async def remove_authorized_user(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    target_user_id = body.get("user_id")
    target_discord_id = str(body.get("discord_id") or "").strip() or None
    if target_user_id is None and not target_discord_id:
        abort_problem(422, "Missing target", "Provide 'user_id' or 'discord_id'.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            caller_role = assert_group_admin(
                s, user_id, group_id, manageable_guild_ids(user_id), user=user
            )

            # Resolve the target across both stores.
            target = None
            if target_user_id is not None:
                target = s.query(User).filter(User.user_id == int(target_user_id)).first()
            elif target_discord_id:
                target = s.query(User).filter(User.discord_id == target_discord_id).first()
            discord_id = target_discord_id or (
                str(target.discord_id) if (target and target.discord_id) else None
            )

            removed = False
            grant = None
            if target is not None:
                grant = (
                    s.query(GroupAdmin)
                    .filter(
                        GroupAdmin.group_id == group_id,
                        GroupAdmin.user_id == target.user_id,
                    )
                    .first()
                )
            if grant is not None:
                if grant.role == "owner":
                    if caller_role != "owner":
                        abort_problem(403, "Owners only", "Only an owner can remove an owner.")
                    owner_grants = (
                        s.query(GroupAdmin)
                        .filter(GroupAdmin.group_id == group_id, GroupAdmin.role == "owner")
                        .count()
                    )
                    if grant.user_id == user_id and owner_grants <= 1:
                        abort_problem(
                            409,
                            "Last owner",
                            "You are this group's only owner — transfer ownership first.",
                        )
                s.delete(grant)
                removed = True

            if discord_id:
                row, ids = _load_authed_ids(s, group_id)
                if discord_id in ids:
                    ids.remove(discord_id)
                    _store_authed_ids(s, group_id, row, ids)
                    removed = True

            if not removed:
                abort_problem(404, "Not authorized", "That user is not on the authorized list.")
            s.commit()
            return {"users": _authorized_entries(s, group_id)}

    return jsonify(await asyncio.to_thread(_apply))


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
