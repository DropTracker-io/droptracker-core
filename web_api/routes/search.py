"""Task 04 — search.

GET /api/v1/search?q=            -> SearchResults (combined; used by web /search)
GET /api/v1/players/search?q=    -> [PlayerSummary]
GET /api/v1/groups/search?q=     -> [{ id, name, member_count }]
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request

from db import Player, Group
from web_api.common import (
    db_session,
    money,
    period_to_partition,
    player_month_total,
    with_cache_headers,
)

search_bp = Blueprint("v1_search", __name__)

LIMIT_EACH = 10


def _search_players(s, q, partition):
    rows = (
        s.query(Player.player_id, Player.player_name)
        .filter(Player.player_name.ilike(f"%{q}%"))
        .order_by(Player.player_name.asc())
        .limit(LIMIT_EACH)
        .all()
    )
    out = []
    for pid, name in rows:
        out.append({"id": pid, "name": name, "total_loot": money(player_month_total(pid, partition))})
    return out


def _search_groups(s, q):
    rows = (
        s.query(Group.group_id, Group.group_name)
        .filter(Group.group_name.ilike(f"%{q}%"))
        .filter(Group.group_id > 2)
        .order_by(Group.group_name.asc())
        .limit(LIMIT_EACH)
        .all()
    )
    out = []
    for gid, name in rows:
        g = s.query(Group).filter(Group.group_id == gid).first()
        member_count = g.get_player_count(session_to_use=s) if g else 0
        out.append({"id": gid, "name": name, "member_count": int(member_count or 0)})
    return out


@search_bp.get("/search")
async def combined_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"players": [], "groups": []})

    def _load():
        with db_session() as s:
            partition = period_to_partition("all")
            return {"players": _search_players(s, q, partition), "groups": _search_groups(s, q)}

    return with_cache_headers(jsonify(await asyncio.to_thread(_load)), max_age=10)


@search_bp.get("/players/search")
async def players_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    def _load():
        with db_session() as s:
            return _search_players(s, q, period_to_partition("all"))

    return jsonify(await asyncio.to_thread(_load))


@search_bp.get("/groups/search")
async def groups_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    def _load():
        with db_session() as s:
            return _search_groups(s, q)

    return jsonify(await asyncio.to_thread(_load))
