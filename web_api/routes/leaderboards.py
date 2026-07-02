"""Task 04 — leaderboards.

GET /api/v1/leaderboards/players?period=&scope=&page=&limit=
GET /api/v1/leaderboards/groups?period=&page=&limit=

Players read the canonical Redis sorted set directly (paged ZREVRANGE). Groups
currently recompute per-partition totals across groups (cached), matching the
legacy `/top_groups` behavior; Task 07 Part B replaces this with a precomputed
per-partition group sorted set.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request

from db import Player, Group
from web_api.common import (
    cache_get,
    cache_set,
    db_session,
    decode_member,
    group_totals_key,
    leaderboard_key,
    money,
    npc_leaderboard_key,
    parse_page,
    period_to_partition,
    player_list_loot_sum,
    resolve_period,
    with_cache_headers,
    _rc,
)

leaderboards_bp = Blueprint("v1_leaderboards", __name__)

GROUP_TOTALS_TTL = 30.0


@leaderboards_bp.get("/leaderboards/players")
async def leaderboards_players():
    period = request.args.get("period", "all")
    scope = request.args.get("scope", "global")
    page, limit = parse_page(request)
    token = resolve_period(period)

    group_id = None
    npc_id = None
    if scope.startswith("group:"):
        try:
            group_id = int(scope.split(":", 1)[1])
        except Exception:
            group_id = None
    elif scope.startswith("npc:"):
        try:
            npc_id = int(scope.split(":", 1)[1])
        except Exception:
            npc_id = None

    if npc_id is not None:
        key = npc_leaderboard_key(token, npc_id, group_id=group_id)
    else:
        key = leaderboard_key(token, group_id=group_id)
    conn = _rc()

    entries = []
    total = 0
    if conn is not None:
        def _read():
            start = (page - 1) * limit
            end = start + limit - 1
            raw = conn.zrevrange(key, start, end, withscores=True)
            card = conn.zcard(key)
            return raw, card

        raw, total = await asyncio.to_thread(_read)

        ids = []
        scored = []
        for member_raw, score in raw:
            pid = decode_member(member_raw)
            if pid is None:
                continue
            ids.append(pid)
            scored.append((pid, int(float(score))))

        name_map = {}
        if ids:
            with db_session() as s:
                rows = s.query(Player.player_id, Player.player_name).filter(Player.player_id.in_(ids)).all()
                name_map = {pid: name for pid, name in rows}

        start_rank = (page - 1) * limit
        for i, (pid, loot) in enumerate(scored):
            entries.append({
                "rank": start_rank + i + 1,
                "id": pid,
                "name": name_map.get(pid, f"Player {pid}"),
                "loot": money(loot),
            })

    resp = jsonify({
        "period": token,
        "scope": scope,
        "entries": entries,
        "meta": {"page": page, "limit": limit, "total": int(total)},
    })
    return with_cache_headers(resp, max_age=15)


def _read_group_totals_precomputed(token):
    """Read the precomputed group-total sorted set (`gleaderboard:{token}`,
    maintained by the lootboard generator). Returns a sorted desc list of
    (group_id, score) or None if the set is empty/absent."""
    conn = _rc()
    if conn is None:
        return None
    try:
        raw = conn.zrevrange(group_totals_key(token), 0, -1, withscores=True)
    except Exception:
        return None
    if not raw:
        return None
    out = []
    for member_raw, score in raw:
        gid = decode_member(member_raw)
        if gid is None or gid in (0, 2):
            continue
        out.append((gid, int(float(score))))
    return out or None


def _compute_group_totals(partition: int):
    """Sorted [(group_id, name, total)] desc across all non-system groups.

    Cached per-partition (in-process). O(all groups) — replaced by a precomputed
    Redis sorted set in Task 07 Part B.
    """
    cache_key = f"group_totals:{partition}"
    cached = cache_get(cache_key, GROUP_TOTALS_TTL)
    if cached is not None:
        return cached

    result = []
    with db_session() as s:
        groups = s.query(Group).all()
        for g in groups:
            if g.group_id in (0, 2):  # 0/2 are reserved/global
                continue
            player_ids = [
                pid for (pid,) in s.query(Player.player_id).join(Player.groups)
                .filter(Group.group_id == g.group_id).all()
            ]
            total = player_list_loot_sum(player_ids, partition)
            result.append((g.group_id, g.group_name, total, len(player_ids)))

    result.sort(key=lambda x: x[2], reverse=True)
    cache_set(cache_key, result)
    return result


@leaderboards_bp.get("/leaderboards/groups")
async def leaderboards_groups():
    period = request.args.get("period", "all")
    page, limit = parse_page(request)
    token = resolve_period(period)
    partition = period_to_partition(period)

    def _load():
        # Prefer the precomputed group-total set (O(page), no full recompute).
        precomputed = _read_group_totals_precomputed(token)
        if precomputed is not None:
            total_count = len(precomputed)
            start = (page - 1) * limit
            window = precomputed[start:start + limit]
            gids = [gid for gid, _ in window]
            names = {}
            if gids:
                with db_session() as s:
                    rows = (
                        s.query(Group.group_id, Group.group_name)
                        .filter(Group.group_id.in_(gids))
                        .all()
                    )
                    names = {gid: name for gid, name in rows}
            entries = [
                {
                    "rank": start + i + 1,
                    "id": gid,
                    "name": names.get(gid, f"Group {gid}"),
                    "loot": money(total),
                }
                for i, (gid, total) in enumerate(window)
            ]
            return entries, total_count

        # Fallback: compute across members (cached). Used for periods the
        # precompute doesn't cover.
        totals = _compute_group_totals(partition)
        start = (page - 1) * limit
        window = totals[start:start + limit]
        entries = [
            {
                "rank": start + i + 1,
                "id": gid,
                "name": name,
                "loot": money(total),
            }
            for i, (gid, name, total, _members) in enumerate(window)
        ]
        return entries, len(totals)

    entries, total_count = await asyncio.to_thread(_load)

    resp = jsonify({
        "period": token,
        "scope": "groups",
        "entries": entries,
        "meta": {"page": page, "limit": limit, "total": total_count},
    })
    return with_cache_headers(resp, max_age=15)
