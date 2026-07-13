"""Task 04 — search.

GET /api/v1/search?q=            -> SearchResults (combined; used by web /search)
GET /api/v1/players/search?q=    -> [PlayerSummary]
GET /api/v1/groups/search?q=     -> [{ id, name, member_count }]
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request
from sqlalchemy import text

from db import Player, Group, User
from utils.npc_names import npc_match_key, npc_match_variants, npc_slug_sql_expr
from web_api.common import (
    db_session,
    money,
    period_to_partition,
    player_month_total,
    with_cache_headers,
)
from web_api.flair import group_flairs

search_bp = Blueprint("v1_search", __name__)

LIMIT_EACH = 10

IMG_BASE = "https://www.droptracker.io/img"


def _search_players(s, q, partition):
    # Privacy: skip hidden accounts and accounts of hidden users. IS NOT TRUE
    # keeps rows with NULL flags / no owning user.
    rows = (
        s.query(Player.player_id, Player.player_name)
        .outerjoin(User, User.user_id == Player.user_id)
        .filter(Player.player_name.ilike(f"%{q}%"))
        .filter(Player.hidden.isnot(True))
        .filter(User.hidden.isnot(True))
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
    flair_map = group_flairs(s, [row["id"] for row in out])
    for row in out:
        flair = flair_map.get(row["id"])
        if flair:
            row["flair"] = flair
    return out


def _primary_npc(s, name_or_slug):
    """(npc_id, npc_name) of the primary row for this boss's match key — the
    variant that actually has tracked data, then lowest id. Same rule as
    /resolve, so search hits and nice URLs land on the same page."""
    from sqlalchemy import bindparam

    variants = npc_match_variants(name_or_slug)
    if not variants:
        return None
    expr = npc_slug_sql_expr("npc_name")
    return s.execute(
        text(
            f"SELECT npc_id, npc_name, "
            f"       EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
            f"              WHERE t.npc_id = npc_list.npc_id) AS tracked "
            f"FROM npc_list WHERE {expr} IN :variants "
            f"ORDER BY tracked DESC, npc_id ASC LIMIT 1"
        ).bindparams(bindparam("variants", expanding=True)),
        {"variants": variants},
    ).fetchone()


def _search_npcs(s, q):
    """NPCs by name. Hits collapse by MATCH KEY (slug + "The " article +
    aliases), not exact name, and each surfaces as its primary page — so
    "Chambers of Xeric (Challenge mode)" and "... Challenge Mode" are one
    result, and searching "hunllef" lands on The Gauntlet (suggestion #50)."""
    rows = s.execute(
        text(
            "SELECT n.npc_id, n.npc_name, "
            "       EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
            "              WHERE t.npc_id = n.npc_id) AS tracked "
            "FROM npc_list n WHERE n.npc_name LIKE :pat "
            "ORDER BY tracked DESC, n.npc_name ASC, n.npc_id ASC LIMIT :lim"
        ),
        {"pat": f"%{q}%", "lim": LIMIT_EACH * 4},
    ).fetchall()
    out = []
    seen_keys = set()
    for nid, name, _tracked in rows:
        key = npc_match_key(name)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        primary = _primary_npc(s, name)
        pid, pname = (int(primary[0]), primary[1]) if primary else (int(nid), name)
        out.append({"id": pid, "name": pname, "icon_url": f"{IMG_BASE}/npcdb/{pid}.png"})
        if len(out) >= LIMIT_EACH:
            break
    # tracked-first ordering serves collapse priority; present alphabetically.
    out.sort(key=lambda r: r["name"].lower())
    return out


def _search_items(s, q):
    """Items by name. The catalog holds many variants per name (noted /
    stack-size / cosmetic duplicates), so rank ids that have actually been
    received (tracked in player_item_hourly_totals) first and collapse
    duplicate names to the best-ranked id."""
    rows = s.execute(
        text(
            "SELECT i.item_id, i.item_name, "
            "       EXISTS(SELECT 1 FROM player_item_hourly_totals t "
            "              WHERE t.item_id = i.item_id) AS tracked "
            "FROM items i WHERE i.item_name LIKE :pat AND i.noted = 0 "
            "ORDER BY tracked DESC, i.item_name ASC, i.item_id ASC LIMIT :lim"
        ),
        {"pat": f"%{q}%", "lim": LIMIT_EACH * 4},
    ).fetchall()
    out = []
    seen_names = set()
    for iid, name, _tracked in rows:
        if name in seen_names:
            continue
        seen_names.add(name)
        out.append({"id": int(iid), "name": name, "icon_url": f"{IMG_BASE}/itemdb/{iid}.png"})
        if len(out) >= LIMIT_EACH:
            break
    return out


@search_bp.get("/search")
async def combined_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"players": [], "groups": [], "npcs": [], "items": []})

    def _load():
        with db_session() as s:
            partition = period_to_partition("all")
            return {
                "players": _search_players(s, q, partition),
                "groups": _search_groups(s, q),
                "npcs": _search_npcs(s, q),
                "items": _search_items(s, q),
            }

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
