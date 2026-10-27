"""Clan rank → Discord role sync (services/rank_roles.py) for the website.

  GET  /api/v1/groups/{id}/rank-roles        the editor's whole state
  PUT  /api/v1/groups/{id}/rank-roles        save (``?dry_run=1`` previews only)
  POST /api/v1/groups/{id}/rank-roles/sync   "Sync now": a full pass that also
                                             lets held-back removals through

The settings are one JSON row (``rank_role_sync``), not registry fields, so
the generic config PATCH never sees them. This route only stores them and asks
the core bot for a pass: the web API never talks to Discord.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify, request

from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import assert_group_admin, current_user_id, json_body, manageable_guild_ids

rank_roles_bp = Blueprint("v1_rank_roles", __name__)


def _rr():
    # Lazy: tests stub the `services` package at import time.
    from services import rank_roles

    return rank_roles


def _role_icon_url(rank: str):
    """The rank's icon from the app emoji the clan chat bridge already uses."""
    try:
        from utils.rank_emojis import emoji_for_rank

        emoji = emoji_for_rank(rank)
    except Exception:
        return None
    if not emoji:
        return None
    emoji_id = emoji.rstrip(">").rsplit(":", 1)[-1]
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.png" if emoji_id.isdigit() else None


def _cached_roles(guild_id):
    """The bot's role cache for the server, or None while it is cold (the
    bot is asked to warm it, as the announcement ping picker does)."""
    if not guild_id:
        return []
    try:
        from utils.redis import redis_client

        conn = redis_client.client
        raw = conn.get(f"guild:{guild_id}:roles")
        if raw:
            roles = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
            if isinstance(roles, list):
                return roles
        conn.sadd("bot:channels:refresh", str(guild_id))
        conn.expire("bot:channels:refresh", 300)
    except Exception:
        pass
    return None


def _group_facts(s, group_id: int):
    from db.models import Group

    group = s.query(Group).filter(Group.group_id == group_id).first()
    if group is None:
        abort_problem(404, "Group not found")
    rr = _rr()
    guild_id = rr._snowflake(group.guild_id)
    conflict = None
    if guild_id:
        owner = rr.sync_owner_group(s, guild_id, group_id)
        if owner is not None and owner != group_id:
            other = s.query(Group.group_name).filter(Group.group_id == owner).first()
            conflict = {"group_id": owner, "group_name": other[0] if other else None}
    return group, guild_id, conflict


def _preview(config, roster, rank_map) -> dict:
    """Who would get which role, from our data alone (the bot checks Discord)."""
    rr = _rr()
    if rank_map is None:
        return {"by_rank": {}, "linked": 0, "with_role": 0}
    desired = rr.desired_roles(config, roster, rank_map, include_pending=True)
    by_role = {}
    for roles in desired.values():
        for role_id in roles:
            by_role[role_id] = by_role.get(role_id, 0) + 1
    by_rank = {}
    for entry, role_id in zip(config["ranks"], rr.effective_roles(config, include_pending=True)):
        if role_id:
            by_rank[entry["rank"]] = by_role.get(role_id, 0)
    return {"by_rank": by_rank, "linked": len(desired),
            "with_role": sum(1 for roles in desired.values() if roles)}


def _payload(s, group_id: int, config=None) -> dict:
    rr = _rr()
    group, guild_id, conflict = _group_facts(s, group_id)
    stored = rr.load_config_row(s, group_id)
    config = config or stored
    roster = rr.load_roster(s, group_id)
    rank_map = rr.read_rank_map(group.wom_id)
    names = [name for _, name in roster]
    counts = rr.rank_counts(names, rank_map or {})
    linked_counts = rr.rank_counts(
        [name for did, name in roster if rr._snowflake(did)], rank_map or {})
    # Ranks with no members are listed only when the leader already placed
    # them, so the list is this clan's ranks, not all ~270.
    order = rr.default_rank_order(counts, [e["rank"] for e in config["ranks"]])
    ranks = [{
        "rank": rank,
        "label": rr.rank_label(rank),
        "icon_url": _role_icon_url(rank),
        "members": counts.get(rank, 0),
        "linked_members": linked_counts.get(rank, 0),
    } for rank in order]
    roles = _cached_roles(guild_id)
    return {
        "group_id": group_id,
        "guild_linked": bool(guild_id),
        "wom_linked": bool(group.wom_id),
        "has_rank_data": rank_map is not None,
        "conflict": conflict,
        "config": config,
        "ranks": ranks,
        "roles": [{"id": str(r.get("id")), "name": r.get("name") or "",
                   "position": int(r.get("position") or 0),
                   "blocked": rr.role_permission_problem(r.get("permissions"))}
                  for r in roles or ()],
        "roles_stale": roles is None,
        "roster": {"members": len(names),
                   "linked": len({rr._snowflake(d) for d, _ in roster if rr._snowflake(d)})},
        "preview": _preview(config, roster, rank_map),
        "status": rr.read_status(group_id),
        "limits": {"max_ranks": rr.MAX_RANKS, "max_role_name": rr.MAX_ROLE_NAME,
                   "delete_grace_days": rr.DELETE_GRACE_SECONDS // 86400},
    }


@rank_roles_bp.get("/groups/<int:group_id>/rank-roles")
async def get_rank_roles(group_id: int):
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id))
            return _payload(s, group_id)

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


@rank_roles_bp.put("/groups/<int:group_id>/rank-roles")
async def put_rank_roles(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    dry_run = str(request.args.get("dry_run") or "").lower() in ("1", "true", "yes")

    def _run():
        from db.models import AuditLog

        rr = _rr()
        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id))
            group, guild_id, conflict = _group_facts(s, group_id)
            stored = rr.load_config_row(s, group_id)
            try:
                config = rr.validate_config(body, guild_id=guild_id, stored=stored,
                                            roles=_cached_roles(guild_id) or None)
            except rr.RankRoleConfigError as exc:
                abort_problem(422, "Rank roles can't be saved", "; ".join(exc.errors),
                              extra={"errors": exc.errors})
            if config["enabled"]:
                problems = []
                if not guild_id:
                    problems.append("Link a Discord server to this group first.")
                if not group.wom_id:
                    problems.append("Link a WiseOldMan group first: clan ranks come from it.")
                if conflict:
                    problems.append(f"{conflict.get('group_name') or 'Another group'} already "
                                    "syncs rank roles in this Discord server. Only one group "
                                    "per server can.")
                if problems:
                    abort_problem(422, "Rank roles can't be turned on", " ".join(problems),
                                  extra={"errors": problems})
            if dry_run:
                return _payload(s, group_id, config=config)
            if rr.dump_config(config) != rr.dump_config(stored):
                rr.save_config(s, group_id, config)
                s.add(AuditLog(actor_user_id=user_id, group_id=group_id,
                               action="config.update", target=f"group_configurations.{rr.CONFIG_KEY}",
                               before=rr.dump_config(stored)[:60000],
                               after=rr.dump_config(config)[:60000]))
                s.commit()
                try:
                    from utils import group_config

                    group_config.invalidate(group_id, rr.CONFIG_KEY)
                except Exception:
                    pass
            if config["enabled"] or stored["enabled"]:
                rr.request_sync(group_id, "full")
            return _payload(s, group_id)

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


@rank_roles_bp.post("/groups/<int:group_id>/rank-roles/sync")
async def sync_rank_roles(group_id: int):
    user_id = current_user_id()

    def _run():
        rr = _rr()
        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id))
            if not rr.load_config_row(s, group_id)["enabled"]:
                abort_problem(409, "Rank roles are off", "Turn rank roles on and save first.")
        return {"ok": bool(rr.request_sync(group_id, "force"))}

    return private_no_store(jsonify(await asyncio.to_thread(_run)))
