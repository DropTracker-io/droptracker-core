"""Slug → entity resolution for the web front-end's "nice URLs".

  GET /api/v1/resolve/<kind>?slug=<slug>   (public, cached)

``kind`` ∈ {group, player, npc, item}. Returns the single matching entity, or —
when a group/player name is shared by several *visible* entities — a candidate
list so the front-end can render a disambiguation page. NPC/item duplicate
names collapse to one primary id (the same rule search.py uses), so those never
disambiguate.

Slugs are computed on the fly from the current name (``web_api.common.slugify``
/ ``slug_sql_expr``); there is no slug column. The tables involved are small
(groups ~hundreds, npcs/items ~thousands), so the expression scan is cheap, and
every response is cached for a few minutes.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request
from sqlalchemy import text

from db import Group
from web_api.common import (
    abort_problem,
    db_session,
    money,
    period_to_partition,
    player_month_total,
    slug_sql_expr,
    slugify,
    with_cache_headers,
)
from web_api.flair import group_flairs

resolve_bp = Blueprint("v1_resolve", __name__)

IMG_BASE = "https://www.droptracker.io/img"
_KINDS = ("group", "player", "npc", "item")
_MAX_CANDIDATES = 25


def _resolve_npc(s, slug: str):
    """NPC variants collapse to one primary: duplicate names, spelling
    variants (same slug), "The " article variants and outright aliases
    (Crystalline Hunllef → The Gauntlet) all resolve to the id that actually
    has tracked data, then lowest id — so a stray empty variant row never
    shadows the real page (suggestion #50)."""
    from sqlalchemy import bindparam

    from utils.npc_names import npc_match_variants

    variants = npc_match_variants(slug)
    if not variants:
        return None, []
    expr = slug_sql_expr("npc_name")
    row = s.execute(
        text(
            f"SELECT npc_id, npc_name, "
            f"       EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
            f"              WHERE t.npc_id = npc_list.npc_id) AS tracked "
            f"FROM npc_list WHERE {expr} IN :variants "
            f"ORDER BY tracked DESC, npc_id ASC LIMIT 1"
        ).bindparams(bindparam("variants", expanding=True)),
        {"variants": variants},
    ).fetchone()
    if not row:
        return None, []
    nid, name, _tracked = row
    match = {"id": int(nid), "name": name, "icon_url": f"{IMG_BASE}/npcdb/{nid}.png"}
    return match, [match]


def _resolve_item(s, slug: str):
    """Item name variants collapse to the id that has actually been received
    (tracked first), then lowest id — matching search.py."""
    expr = slug_sql_expr("item_name")
    row = s.execute(
        text(
            f"SELECT item_id, item_name, "
            f"       EXISTS(SELECT 1 FROM player_item_hourly_totals t WHERE t.item_id = items.item_id) AS tracked "
            f"FROM items WHERE noted = 0 AND {expr} = :slug "
            f"ORDER BY tracked DESC, item_id ASC LIMIT 1"
        ),
        {"slug": slug},
    ).fetchone()
    if not row:
        return None, []
    iid, name, _tracked = row
    match = {"id": int(iid), "name": name, "icon_url": f"{IMG_BASE}/itemdb/{iid}.png"}
    return match, [match]


def _resolve_group(s, slug: str):
    expr = slug_sql_expr("group_name")
    rows = s.execute(
        text(
            f"SELECT group_id, group_name FROM groups "
            f"WHERE group_id > 2 AND {expr} = :slug ORDER BY group_id ASC LIMIT :lim"
        ),
        {"slug": slug, "lim": _MAX_CANDIDATES},
    ).fetchall()
    if not rows:
        return None, []
    candidates = []
    for gid, name in rows:
        g = s.query(Group).filter(Group.group_id == gid).first()
        cand = {
            "id": int(gid),
            "name": name,
            "member_count": int((g.get_player_count(session_to_use=s) if g else 0) or 0),
        }
        if g and g.icon_url:
            cand["icon_url"] = g.icon_url
        if g and g.date_added:
            try:
                cand["created_ts"] = int(g.date_added.timestamp())
            except Exception:
                pass
        candidates.append(cand)
    flair_map = group_flairs(s, [c["id"] for c in candidates])
    for c in candidates:
        fl = flair_map.get(c["id"])
        if fl:
            c["flair"] = fl
    match = candidates[0] if len(candidates) == 1 else None
    return match, candidates


def _resolve_player(s, slug: str):
    expr = slug_sql_expr("p.player_name")
    rows = s.execute(
        text(
            f"SELECT p.player_id, p.player_name FROM players p "
            f"LEFT JOIN users u ON u.user_id = p.user_id "
            f"WHERE p.hidden IS NOT TRUE AND u.hidden IS NOT TRUE AND {expr} = :slug "
            f"ORDER BY p.player_id ASC LIMIT :lim"
        ),
        {"slug": slug, "lim": _MAX_CANDIDATES},
    ).fetchall()
    if not rows:
        return None, []
    partition = period_to_partition("all")
    candidates = [
        {"id": int(pid), "name": name, "total_loot": money(player_month_total(int(pid), partition))}
        for pid, name in rows
    ]
    match = candidates[0] if len(candidates) == 1 else None
    return match, candidates


_RESOLVERS = {
    "group": _resolve_group,
    "player": _resolve_player,
    "npc": _resolve_npc,
    "item": _resolve_item,
}


@resolve_bp.get("/resolve/<kind>")
async def resolve(kind: str):
    if kind not in _KINDS:
        abort_problem(404, "Unknown kind", f"Cannot resolve '{kind}'.")
    # Re-slugify the incoming param so a stray raw name / off-by-punctuation
    # value still resolves; idempotent for a well-formed slug.
    slug = slugify(request.args.get("slug") or "")
    if not slug:
        return with_cache_headers(
            jsonify({"kind": kind, "slug": "", "match": None, "candidates": []}), max_age=300
        )

    def _load():
        with db_session() as s:
            match, candidates = _RESOLVERS[kind](s, slug)
            return {"kind": kind, "slug": slug, "match": match, "candidates": candidates}

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=300)
