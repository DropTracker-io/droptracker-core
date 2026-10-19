"""PvP standings, kills and boards for the website.

GET  /api/v1/pvp/leaderboard?period=&scope=global|group:{id}&metric=loot|kills|deaths&page=&limit=
GET  /api/v1/pvp/recent?scope=global|group:{id}&limit=
GET  /api/v1/groups/{id}/pvp?period=
GET  /api/v1/players/{id}/pvp?period=
POST /api/v1/admin/pvp/kills/{id}/hidden   {"hidden": bool}   (superadmin)

Standings come from the ``pvp:`` Redis namespace (``services/pvp_standings``);
kill detail and history come from ``pvp_kills``. Public reads drop players
who chose to be hidden, the same way the drop leaderboards do.
"""
from __future__ import annotations

import asyncio
import os

from quart import Blueprint, jsonify, request

from web_api.common import (
    abort_problem,
    db_session,
    hidden_player_ids,
    money,
    parse_page,
    resolve_period,
    with_cache_headers,
)
from web_api.deps import assert_superadmin, current_user_id, load_user

pvp_bp = Blueprint("v1_pvp", __name__)

IMG_BASE = "https://www.droptracker.io/img"
GLOBAL_GROUP_ID = 2
_METRICS = ("loot", "kills", "deaths")
_RECENT_DEFAULT = 20
_RECENT_MAX = 50


def _standings():
    # Lazy: the unit-test conftest stubs the ``services`` package.
    import importlib

    return importlib.import_module("services.pvp_standings")


def _parse_scope(raw: str | None) -> int | None:
    """``global`` -> None, ``group:{id}`` -> id. Anything else is a 400."""
    raw = (raw or "global").strip().lower()
    if raw == "global":
        return None
    if raw.startswith("group:"):
        try:
            gid = int(raw.split(":", 1)[1])
        except ValueError:
            gid = None
        if gid is not None:
            return None if gid == GLOBAL_GROUP_ID else gid
    abort_problem(400, "Bad Request", "scope must be 'global' or 'group:{id}'.")


def _kd(kills: int, deaths: int) -> float | None:
    if deaths <= 0:
        return None if kills <= 0 else float(kills)
    return round(kills / deaths, 2)


def _board_image(group_id: int) -> dict | None:
    from lootboard import schedule

    try:
        mtime = os.path.getmtime(schedule.board_path(group_id, schedule.PVP))
    except OSError:
        return None
    return {
        "url": f"{IMG_BASE}/clans/{int(group_id)}/lb/pvp_lootboard.png?v={int(mtime)}",
        "updated_at": int(mtime),
    }


def _names(s, ids) -> dict:
    from db import Player

    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return {}
    return {
        int(pid): name
        for pid, name in s.query(Player.player_id, Player.player_name)
        .filter(Player.player_id.in_(ids)).all()
    }


def _kill_row(kill, items, names: dict, hidden: set) -> dict:
    victim_id = kill.victim_player_id
    return {
        "id": int(kill.id),
        "player": {"id": int(kill.player_id), "name": names.get(int(kill.player_id), "Unknown")},
        "victim": {
            "name": kill.victim_name,
            # A hidden victim is named (the game showed the name) but not linked.
            "id": int(victim_id) if victim_id is not None and victim_id not in hidden else None,
            "combat_level": kill.victim_combat_level,
        },
        "value": money(kill.total_value),
        "item_count": int(kill.item_count or 0),
        "items": [
            {
                "item_id": int(i.item_id),
                "name": i.item_name,
                "quantity": int(i.quantity or 0),
                "value": money(int(i.value or 0) * int(i.quantity or 0)),
                "icon_url": f"{IMG_BASE}/itemdb/{int(i.item_id)}.png",
            }
            for i in sorted(items, key=lambda i: (i.value or 0) * (i.quantity or 0), reverse=True)
        ],
        "world": kill.world,
        "wilderness_level": kill.wilderness_level,
        "location": kill.location,
        "image_url": kill.image_url,
        "date": int(kill.date_added.timestamp()) if kill.date_added else None,
    }


def _recent_kills(s, *, player_id=None, group_id=None, limit=_RECENT_DEFAULT) -> list:
    """Latest counted kills, from the database (the detail Redis does not hold)."""
    from db.models import PvpKill, PvpKillGroup, PvpKillItem

    q = s.query(PvpKill).filter(PvpKill.hidden.is_(False), PvpKill.excluded_reason.is_(None))
    if player_id is not None:
        q = q.filter(PvpKill.player_id == int(player_id))
    if group_id is not None:
        q = q.join(PvpKillGroup, PvpKillGroup.kill_id == PvpKill.id).filter(
            PvpKillGroup.group_id == int(group_id), PvpKillGroup.counted.is_(True))
    kills = q.order_by(PvpKill.id.desc()).limit(limit).all()
    if not kills:
        return []
    items: dict = {}
    for item in s.query(PvpKillItem).filter(PvpKillItem.kill_id.in_([k.id for k in kills])).all():
        items.setdefault(int(item.kill_id), []).append(item)
    hidden = hidden_player_ids()
    names = _names(s, [k.player_id for k in kills])
    return [
        _kill_row(k, items.get(int(k.id), []), names, hidden)
        for k in kills if int(k.player_id) not in hidden
    ]


@pvp_bp.get("/pvp/leaderboard")
async def pvp_leaderboard():
    token = resolve_period(request.args.get("period", "month"))
    group_id = _parse_scope(request.args.get("scope"))
    metric = (request.args.get("metric") or "loot").strip().lower()
    if metric not in _METRICS:
        abort_problem(400, "Bad Request", "metric must be loot, kills or deaths.")
    page, limit = parse_page(request)
    standings = _standings()

    def _read():
        rows, total = standings.top(metric, token, group_id, limit=limit,
                                    offset=(page - 1) * limit)
        ids = [pid for pid, _ in rows]
        others = {m: standings.scores_for(m, token, ids, group_id)
                  for m in _METRICS if m != metric}
        hidden = hidden_player_ids()
        with db_session() as s:
            names = _names(s, ids)
        return rows, total, others, hidden, names

    rows, total, others, hidden, names = await asyncio.to_thread(_read)
    start_rank = (page - 1) * limit
    entries = []
    for pos, (pid, score) in enumerate(rows):
        if pid in hidden:
            continue
        line = {metric: score}
        for m, scores in others.items():
            line[m] = scores.get(pid, 0)
        entries.append({
            "rank": start_rank + pos + 1,
            "id": pid,
            "name": names.get(pid, f"Player {pid}"),
            "loot": money(line.get("loot", 0)),
            "kills": int(line.get("kills", 0)),
            "deaths": int(line.get("deaths", 0)),
            "kd": _kd(int(line.get("kills", 0)), int(line.get("deaths", 0))),
        })
    resp = jsonify({
        "period": token,
        "scope": f"group:{group_id}" if group_id is not None else "global",
        "metric": metric,
        "entries": entries,
        "meta": {"page": page, "limit": limit, "total": int(total)},
    })
    return with_cache_headers(resp, max_age=15)


@pvp_bp.get("/pvp/recent")
async def pvp_recent():
    group_id = _parse_scope(request.args.get("scope"))
    try:
        limit = max(1, min(int(request.args.get("limit", _RECENT_DEFAULT)), _RECENT_MAX))
    except ValueError:
        limit = _RECENT_DEFAULT

    def _read():
        with db_session() as s:
            return _recent_kills(s, group_id=group_id if group_id is not None else GLOBAL_GROUP_ID,
                                 limit=limit)

    kills = await asyncio.to_thread(_read)
    return with_cache_headers(jsonify({"kills": kills}), max_age=15)


@pvp_bp.get("/groups/<int:group_id>/pvp")
async def group_pvp(group_id: int):
    from db import Group

    token = resolve_period(request.args.get("period", "month"))
    standings = _standings()
    scope = None if group_id == GLOBAL_GROUP_ID else group_id

    def _read():
        with db_session() as s:
            group = s.query(Group.group_id, Group.group_name).filter(
                Group.group_id == group_id).first()
            if group is None:
                return None
            hidden = hidden_player_ids()
            top_loot, players = standings.top("loot", token, scope, limit=10)
            top_kills, _ = standings.top("kills", token, scope, limit=10)
            kills_total = standings.board_sum("kills", token, scope)
            names = _names(s, [p for p, _ in top_loot] + [p for p, _ in top_kills])
            recent = _recent_kills(s, group_id=group_id, limit=10)
            return {
                "group": {"id": int(group.group_id), "name": group.group_name},
                "period": token,
                "totals": {
                    "loot": money(standings.group_total(group_id, token)),
                    "kills": int(kills_total),
                    "players": int(players),
                },
                "top_loot": [
                    {"rank": i + 1, "id": pid, "name": names.get(pid, f"Player {pid}"),
                     "loot": money(score)}
                    for i, (pid, score) in enumerate(p for p in top_loot if p[0] not in hidden)
                ],
                "top_kills": [
                    {"rank": i + 1, "id": pid, "name": names.get(pid, f"Player {pid}"),
                     "kills": int(score)}
                    for i, (pid, score) in enumerate(p for p in top_kills if p[0] not in hidden)
                ],
                "recent": recent,
                "board": _board_image(group_id),
            }

    data = await asyncio.to_thread(_read)
    if data is None:
        abort_problem(404, "Not Found", "Group not found.")
    return with_cache_headers(jsonify(data), max_age=15)


@pvp_bp.get("/players/<int:player_id>/pvp")
async def player_pvp(player_id: int):
    from sqlalchemy import text

    from db import Player

    token = resolve_period(request.args.get("period", "month"))
    standings = _standings()

    def _read():
        if player_id in hidden_player_ids():
            return None
        with db_session() as s:
            player = s.query(Player.player_id, Player.player_name).filter(
                Player.player_id == player_id).first()
            if player is None:
                return None
            period_line = standings.player_line(player_id, token)
            all_line = standings.player_line(player_id, "all")
            deaths = s.execute(text(
                "SELECT id, source, location, value_lost, date_added FROM player_deaths "
                "WHERE player_id = :pid AND killer_type = 'player' "
                "ORDER BY id DESC LIMIT 10"
            ), {"pid": player_id}).all()
            return {
                "player": {"id": int(player.player_id), "name": player.player_name},
                "period": token,
                "stats": {
                    "loot": money(period_line["loot"]),
                    "kills": period_line["kills"],
                    "deaths": period_line["deaths"],
                    "kd": _kd(period_line["kills"], period_line["deaths"]),
                    "rank": period_line["rank"],
                },
                "all_time": {
                    "loot": money(all_line["loot"]),
                    "kills": all_line["kills"],
                    "deaths": all_line["deaths"],
                    "kd": _kd(all_line["kills"], all_line["deaths"]),
                    "rank": all_line["rank"],
                },
                "recent_kills": _recent_kills(s, player_id=player_id, limit=10),
                "recent_deaths": [
                    {
                        "id": int(d.id),
                        "killer": d.source,
                        "location": d.location,
                        "value_lost": money(d.value_lost) if d.value_lost is not None else None,
                        "date": int(d.date_added.timestamp()) if d.date_added else None,
                    }
                    for d in deaths
                ],
            }

    data = await asyncio.to_thread(_read)
    if data is None:
        abort_problem(404, "Not Found", "Player not found.")
    return with_cache_headers(jsonify(data), max_age=15)


@pvp_bp.post("/admin/pvp/kills/<int:kill_id>/hidden")
async def admin_hide_pvp_kill(kill_id: int):
    """Hide (or unhide) one kill everywhere: boards, standings, the site."""
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}
    hidden = bool(body.get("hidden", True))
    standings = _standings()

    def _apply():
        from db.models import AuditLog, PvpKill, PvpKillGroup

        with db_session() as s:
            assert_superadmin(load_user(s, user_id))
            kill = s.query(PvpKill).filter(PvpKill.id == kill_id).first()
            if kill is None:
                return None
            if bool(kill.hidden) == hidden:
                return kill.hidden
            counted = [int(g) for (g,) in s.query(PvpKillGroup.group_id).filter(
                PvpKillGroup.kill_id == kill.id, PvpKillGroup.counted.is_(True)).all()]
            s.add(AuditLog(
                actor_user_id=user_id,
                action="pvp.kill.hidden" if hidden else "pvp.kill.shown",
                target=f"pvp_kills.{kill.id}",
                before=str(bool(kill.hidden)).lower(),
                after=str(hidden).lower(),
            ))
            kill.hidden = hidden
            s.commit()
            if kill.excluded_reason is None:
                if hidden:
                    standings.remove_kill(kill, counted)
                else:
                    standings.record_kill(kill, counted)
            return kill.hidden

    result = await asyncio.to_thread(_apply)
    if result is None:
        abort_problem(404, "Not Found", "PvP kill not found.")
    return jsonify({"id": kill_id, "hidden": bool(result)})
