"""The profile's Overview, Skills and Diaries data.

Three public reads, all scoped to one player:

- ``/players/<id>/overview`` — what the account has been doing: skill levels,
  what it gained over the last day / week / month (loot, kills, collection log
  slots, combat achievements, personal bests, quests, pets, XP), and a dated
  feed of recent achievements. Entirely our own data.
- ``/players/<id>/diaries`` — every achievement diary task, with what is known
  about each (``utils/diary_tasks.py``).
- ``/players/<id>/wom-gains`` — Wise Old Man's view of the same periods, for
  XP and boss kills we have no history for. Off unless ``PROFILE_WOM_GAINS``
  is set: it spends WOM's request budget.

The two reads that can leave this box (WikiSync inside ``diaries``, WOM) are
fetched by the browser only when someone opens that tab, are cached for
hours, and fail to "nothing extra" — a profile never waits on a third party.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any, Optional

from quart import Blueprint, jsonify, request
from sqlalchemy import text

from db import Player
from db.models import PlayerDiaryTier, PlayerExperience, PlayerState
from web_api.common import (
    _rc,
    cache_get,
    cache_set,
    db_session,
    hidden_player_ids,
    money,
    problem,
    with_cache_headers,
)

player_overview_bp = Blueprint("v1_player_overview", __name__)

IMG_BASE = "https://www.droptracker.io/img"

PERIODS = ("day", "week", "month")
_PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}

_OVERVIEW_TTL = 60.0
_FEED_LIMIT = 40
_PER_SOURCE = 15
_TOP_NPCS = 6

# Level-ups are announced for every level; a feed of achievements wants the
# ones a player would mention.
_MIN_FEED_LEVEL = 50

# `notified` holds every drop a group posted, and some groups post everything.
# The feed is for the ones worth a line next to a personal best.
_MIN_FEED_DROP_VALUE = 1_000_000


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().strip('"').strip("'").lower() in ("1", "true", "yes", "on")


def _visible_player(s, player_id: int):
    """The player row, or None when missing or hidden (indistinguishable)."""
    player = s.query(Player).filter(Player.player_id == player_id).first()
    if player is None or bool(player.hidden) or player_id in hidden_player_ids():
        return None
    return player


def _ms_time(ms: Any) -> str:
    try:
        total = int(ms)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    minutes, rest = divmod(total, 60_000)
    seconds = rest / 1000
    hours, minutes = divmod(minutes, 60)
    tail = f"{seconds:04.1f}" if rest % 1000 else f"{int(seconds):02d}"
    return f"{hours}:{minutes:02d}:{tail}" if hours else f"{minutes}:{tail}"


def _ts(value) -> int:
    try:
        return int(value.timestamp()) if value else 0
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# Gains
# --------------------------------------------------------------------------- #

def _cutoffs(now: datetime) -> dict:
    return {p: now - timedelta(days=d) for p, d in _PERIOD_DAYS.items()}


def _count_since(s, table: str, player_id: int, cutoffs: dict, extra: str = "") -> dict:
    """Rows per rolling period from an event table with ``date_added``."""
    row = s.execute(
        text(
            f"SELECT COALESCE(SUM(date_added >= :day), 0), COALESCE(SUM(date_added >= :week), 0), "
            f"COUNT(*) FROM {table} WHERE player_id = :pid AND date_added >= :month {extra}"
        ),
        {"pid": player_id, **cutoffs},
    ).first()
    return {"day": int(row[0] or 0), "week": int(row[1] or 0), "month": int(row[2] or 0)}


def _loot_gains(s, player_id: int, now: datetime) -> dict:
    """Loot value, drop count and the most-farmed NPCs per period.

    From the hourly rollup rather than ``drops``: an index range on
    (player, hour) instead of a scan of the player's month.
    """
    hour = {p: c.strftime("%Y-%m-%d-%H") for p, c in _cutoffs(now).items()}
    rows = s.execute(
        text(
            "SELECT t.npc_id, n.npc_name, "
            "SUM(IF(t.date_hour >= :day, t.total_value, 0)), SUM(IF(t.date_hour >= :day, t.drop_count, 0)), "
            "SUM(IF(t.date_hour >= :week, t.total_value, 0)), SUM(IF(t.date_hour >= :week, t.drop_count, 0)), "
            "SUM(t.total_value), SUM(t.drop_count) "
            "FROM player_npc_hourly_totals t JOIN npc_list n ON n.npc_id = t.npc_id "
            "WHERE t.player_id = :pid AND t.date_hour >= :month "
            "GROUP BY t.npc_id, n.npc_name"
        ),
        {"pid": player_id, **hour},
    ).fetchall()
    out = {}
    for index, period in enumerate(PERIODS):
        value_col, drops_col = 2 + index * 2, 3 + index * 2
        npcs = [
            (int(r[0]), r[1], int(r[value_col] or 0), int(r[drops_col] or 0))
            for r in rows
            if int(r[drops_col] or 0) > 0
        ]
        npcs.sort(key=lambda r: r[2], reverse=True)
        out[period] = {
            "loot": money(sum(r[2] for r in npcs)),
            "drops": sum(r[3] for r in npcs),
            "npcs": len(npcs),
            "top_npcs": [
                {
                    "npc_id": npc_id,
                    "name": name,
                    "loot": money(value),
                    "drops": drops,
                    "icon_url": f"{IMG_BASE}/npcdb/{npc_id}.png",
                }
                for npc_id, name, value, drops in npcs[:_TOP_NPCS]
            ],
        }
    return out


def _gains(s, player_id: int, now: datetime) -> dict:
    cutoffs = _cutoffs(now)
    try:
        loot = _loot_gains(s, player_id, now)
    except Exception:
        loot = {p: {"loot": money(0), "drops": 0, "npcs": 0, "top_npcs": []} for p in PERIODS}
    counts = {}
    for key, table, extra in (
        ("clog", "collection", ""),
        ("combat_achievements", "combat_achievement", ""),
        ("personal_bests", "personal_best", "AND new_pb = 1"),
        ("quests", "quest_completions", ""),
        ("pets", "player_pets", ""),
    ):
        try:
            counts[key] = _count_since(s, table, player_id, cutoffs, extra)
        except Exception:
            counts[key] = {p: 0 for p in PERIODS}
    return {
        period: {**loot[period], **{key: counts[key][period] for key in counts}}
        for period in PERIODS
    }


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

def _skills(s, player_id: int, today: date) -> dict:
    from utils.xp_history import gains_since, skill_summary, skills_from_row

    row = (
        s.query(PlayerExperience)
        .filter(PlayerExperience.player_id == player_id)
        .order_by(PlayerExperience.last_updated.desc())
        .first()
    )
    state = s.query(PlayerState).filter(PlayerState.player_id == player_id).first()
    combat_level = state.combat_level if state else None
    if row is None:
        return {"has_data": False, "skills": [], "total_level": None, "total_xp": None,
                "combat_level": combat_level, "updated_at": None,
                "gains": {p: None for p in PERIODS}}
    xp = skills_from_row(row)
    summary = skill_summary(xp)
    # A row of all zeroes is a level-up submission that never carried XP.
    has_data = summary["total_xp"] > 0
    try:
        from utils.stat_snapshots import xp_baselines

        now = datetime.now()
        baselines, earliest = xp_baselines(s, player_id, now)
        gains = gains_since(xp, baselines, earliest, now)
    except Exception:
        # Before web161a is applied there is no history table to read.
        gains = {p: None for p in PERIODS}
    return {
        "has_data": has_data,
        "skills": summary["skills"] if has_data else [],
        "total_level": summary["total_level"] if has_data else None,
        "total_xp": summary["total_xp"] if has_data else None,
        "combat_level": combat_level,
        "updated_at": _ts(row.last_updated) or None,
        "gains": gains,
    }


# --------------------------------------------------------------------------- #
# Recent achievements feed
# --------------------------------------------------------------------------- #

def _ca_task_index(s) -> dict:
    """task name (casefolded) -> registry entry, for tier and monster."""
    try:
        from web_api.routes.player_state import _combat_achievement_registry

        return {
            str(t.get("name") or "").casefold(): t
            for t in _combat_achievement_registry(s) or []
        }
    except Exception:
        return {}


def notification_feed_items(rows) -> list:
    """Level-ups and kill-count milestones out of ``notification_queue`` rows.

    ``rows`` are ``(notification_type, data_json, created_at)``. The queue has
    one row per group a notification went to, so the same event shows up
    several times; it is keyed here by what happened, not by row.
    """
    out = {}
    for kind, raw, created in rows:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        ts = _ts(created)
        if kind in ("level_up", "total_level_milestone"):
            if kind == "total_level_milestone":
                total = data.get("total_level")
                if not isinstance(total, int):
                    continue
                key = ("total_level", total)
                item = {
                    "type": "total_level",
                    "title": f"{total:,} total level",
                    "detail": "Total level milestone",
                    "icon_url": f"{IMG_BASE}/metrics/overall.png",
                    "ts": ts,
                }
            else:
                skill = str(data.get("skill_name") or "").strip()
                level = data.get("new_level")
                if not skill or not isinstance(level, int) or level < _MIN_FEED_LEVEL:
                    continue
                key = ("level", skill.lower(), level)
                item = {
                    "type": "level",
                    "title": f"{level} {skill}",
                    "detail": "Level up",
                    "icon_url": f"{IMG_BASE}/metrics/{skill.lower()}.png",
                    "ts": ts,
                }
        elif kind == "kc_milestone":
            npc = str(data.get("npc_name") or "").strip()
            milestone = data.get("milestone") or data.get("kill_count")
            if not npc or not isinstance(milestone, int):
                continue
            npc_id = data.get("npc_id")
            key = ("kc", npc.lower(), milestone)
            item = {
                "type": "kc",
                "title": f"{milestone:,} {npc} kills",
                "detail": "First kill" if data.get("is_first_kill") else "Kill count milestone",
                "icon_url": f"{IMG_BASE}/npcdb/{npc_id}.png" if isinstance(npc_id, int) else None,
                "npc_id": npc_id if isinstance(npc_id, int) else None,
                "ts": ts,
            }
        else:
            continue
        # Keep the earliest sighting: that is when it happened.
        if key not in out or (ts and ts < out[key]["ts"]):
            out[key] = item
    return list(out.values())


def _feed(s, player_id: int) -> list:
    items: list = []
    params = {"pid": player_id, "lim": _PER_SOURCE}

    def run(sql: str, extra: Optional[dict] = None):
        try:
            return s.execute(text(sql), {**params, **(extra or {})}).fetchall()
        except Exception:
            return []

    # Drops worth announcing: `notified` holds the ones a group posted.
    seen = set()
    for drop_id, item_id, item_name, value, quantity, npc_id, npc_name, when in run(
        "SELECT d.drop_id, d.item_id, i.item_name, d.value, d.quantity, d.npc_id, n.npc_name, ns.date_added "
        "FROM notified ns JOIN drops d ON d.drop_id = ns.drop_id "
        "LEFT JOIN items i ON i.item_id = d.item_id LEFT JOIN npc_list n ON n.npc_id = d.npc_id "
        "WHERE ns.player_id = :pid AND ns.drop_id IS NOT NULL AND d.hidden = 0 "
        "AND d.value * GREATEST(d.quantity, 1) >= :min_value "
        "ORDER BY ns.date_added DESC LIMIT :lim2",
        {"lim2": _PER_SOURCE * 3, "min_value": _MIN_FEED_DROP_VALUE},
    ):
        if drop_id in seen:
            continue
        seen.add(drop_id)
        items.append({
            "type": "drop",
            "title": item_name or f"Item {item_id}",
            "detail": f"from {npc_name}" if npc_name else "Drop",
            "icon_url": f"{IMG_BASE}/itemdb/{item_id}.png",
            "item_id": int(item_id) if item_id else None,
            "npc_id": int(npc_id) if npc_id else None,
            "value": money((value or 0) * (quantity or 1)),
            "ts": _ts(when),
        })

    for item_id, item_name, npc_id, npc_name, when in run(
        "SELECT c.item_id, i.item_name, c.npc_id, n.npc_name, c.date_added FROM collection c "
        "LEFT JOIN items i ON i.item_id = c.item_id LEFT JOIN npc_list n ON n.npc_id = c.npc_id "
        "WHERE c.player_id = :pid AND c.date_added IS NOT NULL ORDER BY c.date_added DESC LIMIT :lim"
    ):
        items.append({
            "type": "clog",
            "title": item_name or f"Item {item_id}",
            "detail": f"New collection log slot · {npc_name}" if npc_name else "New collection log slot",
            "icon_url": f"{IMG_BASE}/itemdb/{item_id}.png",
            "item_id": int(item_id) if item_id else None,
            "npc_id": int(npc_id) if npc_id else None,
            "ts": _ts(when),
        })

    registry = None
    for task_name, when in run(
        "SELECT task_name, MIN(date_added) AS first_seen FROM combat_achievement "
        "WHERE player_id = :pid AND date_added IS NOT NULL "
        "GROUP BY task_name ORDER BY first_seen DESC LIMIT :lim"
    ):
        if registry is None:
            registry = _ca_task_index(s)
        task = registry.get(str(task_name or "").strip().casefold()) or {}
        tier = task.get("tier")
        monster = task.get("monster")
        detail = " · ".join(x for x in (f"{tier} combat achievement" if tier else "Combat achievement", monster) if x)
        items.append({
            "type": "ca",
            "title": task_name,
            "detail": detail,
            "icon_url": None,
            "tier": tier,
            "ts": _ts(when),
        })

    for npc_id, npc_name, kill_time, best, team_size, when in run(
        "SELECT p.npc_id, n.npc_name, p.kill_time, p.personal_best, p.team_size, p.date_added "
        "FROM personal_best p LEFT JOIN npc_list n ON n.npc_id = p.npc_id "
        "WHERE p.player_id = :pid AND p.date_added IS NOT NULL AND p.new_pb = 1 "
        "ORDER BY p.date_added DESC LIMIT :lim"
    ):
        raw = best if best and (not kill_time or best < kill_time) else kill_time
        size = str(team_size or "").strip()
        size_label = "Solo" if size.lower() in ("solo", "1", "") else f"Team of {size}"
        items.append({
            "type": "pb",
            "title": f"{npc_name or 'Unknown'} personal best",
            "detail": " · ".join(x for x in (_ms_time(raw), size_label) if x),
            "icon_url": f"{IMG_BASE}/npcdb/{npc_id}.png" if npc_id else None,
            "npc_id": int(npc_id) if npc_id else None,
            "ts": _ts(when),
        })

    for quest_name, quest_points, when in run(
        "SELECT quest_name, quest_points, date_added FROM quest_completions "
        "WHERE player_id = :pid AND date_added IS NOT NULL ORDER BY date_added DESC LIMIT :lim"
    ):
        items.append({
            "type": "quest",
            "title": quest_name,
            "detail": f"Quest complete · {quest_points} quest points" if quest_points else "Quest complete",
            "icon_url": None,
            "ts": _ts(when),
        })

    for item_id, pet_name, when in run(
        "SELECT item_id, pet_name, date_added FROM player_pets "
        "WHERE player_id = :pid AND date_added IS NOT NULL ORDER BY date_added DESC LIMIT :lim"
    ):
        items.append({
            "type": "pet",
            "title": pet_name,
            "detail": "New pet",
            "icon_url": f"{IMG_BASE}/itemdb/{item_id}.png" if item_id else None,
            "item_id": int(item_id) if item_id else None,
            "ts": _ts(when),
        })

    items.extend(notification_feed_items(run(
        "SELECT notification_type, data, created_at FROM notification_queue "
        "WHERE player_id = :pid AND notification_type IN ('level_up', 'total_level_milestone', 'kc_milestone') "
        "ORDER BY id DESC LIMIT :lim2",
        {"lim2": _PER_SOURCE * 6},
    )))

    items = [i for i in items if i.get("ts")]
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:_FEED_LIMIT]


@player_overview_bp.get("/players/<int:player_id>/overview")
async def player_overview(player_id: int):
    def _load():
        key = f"poverview:{player_id}"
        cached = cache_get(key, _OVERVIEW_TTL)
        if cached is not None:
            return cached
        now = datetime.now()
        with db_session() as s:
            if _visible_player(s, player_id) is None:
                return None
            payload = {
                "player_id": player_id,
                "generated_at": int(now.timestamp()),
                "skills": _skills(s, player_id, now.date()),
                "gains": _gains(s, player_id, now),
                "activity": _feed(s, player_id),
                # Whether the browser may ask for Wise Old Man's numbers.
                "wom_gains_enabled": _flag("PROFILE_WOM_GAINS"),
            }
        cache_set(key, payload)
        return payload

    payload = await asyncio.to_thread(_load)
    if payload is None:
        return problem(404, "Player not found", f"No player with id {player_id}")
    return with_cache_headers(jsonify(payload), max_age=60)


# --------------------------------------------------------------------------- #
# Diaries
# --------------------------------------------------------------------------- #

@player_overview_bp.get("/players/<int:player_id>/diaries")
async def player_diaries(player_id: int):
    """Every diary task with its state, as far as it can be known."""

    def _load():
        from utils.diary_tasks import merge_diaries

        with db_session() as s:
            player = _visible_player(s, player_id)
            if player is None:
                return None
            name = player.player_name
            counts = {
                (row.area_id, row.tier): row.completed_count
                for row in s.query(PlayerDiaryTier).filter(PlayerDiaryTier.player_id == player_id)
            }
            state = s.query(PlayerState).filter(PlayerState.player_id == player_id).first()
            last_synced = state.last_synced_at.isoformat() if state and state.last_synced_at else None

        # Only a tier that is partly done needs to know *which* tasks; a player
        # with none of those costs the wiki nothing.
        from utils.diary_tasks import tier_totals

        sizes = tier_totals()
        needs_tasks = any(0 < done < sizes.get(key, 0) for key, done in counts.items())
        wikisync = None
        if needs_tasks and _flag("PROFILE_WIKISYNC_DIARIES"):
            from utils.wikisync import diaries_for

            wikisync = diaries_for(name)

        areas = merge_diaries(counts, (wikisync or {}).get("diaries"))
        return {
            "player_id": player_id,
            "has_synced": bool(counts),
            "last_synced": last_synced,
            "areas": areas,
            "completed": sum(a["completed"] for a in areas),
            "total": sum(a["total"] for a in areas),
            "wikisync_used": any(
                t["task_source"] == "wikisync" for a in areas for t in a["tiers"]
            ),
        }

    payload = await asyncio.to_thread(_load)
    if payload is None:
        return problem(404, "Player not found", f"No player with id {player_id}")
    return with_cache_headers(jsonify(payload), max_age=300)


# --------------------------------------------------------------------------- #
# Wise Old Man gains (optional)
# --------------------------------------------------------------------------- #

_WOM_BASE = "https://api.wiseoldman.net/v2/players"
_WOM_HIT_TTL = 3 * 3600
_WOM_MISS_TTL = 20 * 60
_WOM_TIMEOUT = 10
_WOM_ACHIEVEMENTS = 12


def shape_wom_gains(body: Any) -> Optional[dict]:
    """WOM's ``/gained`` response reduced to what the profile shows."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    skills = {}
    total = 0
    for metric, entry in (data.get("skills") or {}).items():
        gained = ((entry or {}).get("experience") or {}).get("gained")
        if not isinstance(gained, (int, float)) or gained <= 0:
            continue
        if metric == "overall":
            total = int(gained)
        else:
            skills[metric] = int(gained)
    bosses = []
    for metric, entry in (data.get("bosses") or {}).items():
        gained = ((entry or {}).get("kills") or {}).get("gained")
        if isinstance(gained, (int, float)) and gained > 0:
            bosses.append({"metric": metric, "kills": int(gained)})
    bosses.sort(key=lambda b: b["kills"], reverse=True)

    def _computed(key):
        value = (((data.get("computed") or {}).get(key) or {}).get("value") or {}).get("gained")
        return round(float(value), 2) if isinstance(value, (int, float)) else 0.0

    return {
        "starts_at": body.get("startsAt"),
        "ends_at": body.get("endsAt"),
        "total_xp": total or sum(skills.values()),
        "skills": skills,
        "bosses": bosses,
        "ehp": _computed("ehp"),
        "ehb": _computed("ehb"),
    }


def shape_wom_achievements(body: Any, limit: int = _WOM_ACHIEVEMENTS) -> list:
    """Dated WOM achievements, newest first. Undated ones (WOM stamps those
    it cannot place at 1970) are dropped: a feed needs a date."""
    out = []
    for entry in body if isinstance(body, list) else []:
        created = str((entry or {}).get("createdAt") or "")
        if not created or created.startswith("1970"):
            continue
        out.append({
            "name": entry.get("name") or "",
            "metric": entry.get("metric") or "",
            "created_at": created,
        })
    out.sort(key=lambda a: a["created_at"], reverse=True)
    return out[:limit]


def _wom_get(path: str) -> Any:
    import requests

    headers = {"User-Agent": "DropTracker.io player-profiles"}
    api_key = (os.getenv("WOM_API_KEY") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    resp = requests.get(f"{_WOM_BASE}/{path}", headers=headers, timeout=_WOM_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


@player_overview_bp.get("/players/<int:player_id>/wom-gains")
async def player_wom_gains(player_id: int):
    period = request.args.get("period") or "week"
    if period not in PERIODS:
        return problem(400, "Bad period", "period must be day, week or month")
    if not _flag("PROFILE_WOM_GAINS"):
        return with_cache_headers(jsonify({"enabled": False, "period": period}), max_age=300)

    def _lookup():
        with db_session() as s:
            player = _visible_player(s, player_id)
            return player.player_name if player is not None else None

    name = await asyncio.to_thread(_lookup)
    if name is None:
        return problem(404, "Player not found", f"No player with id {player_id}")

    cache_key = f"wom:profile_gains:{player_id}:{period}"

    def _cached():
        conn = _rc()
        if conn is None:
            return None
        try:
            raw = conn.get(cache_key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    payload = await asyncio.to_thread(_cached)
    if payload is None:
        # One shared budget for every process that talks to WOM; when it is
        # backed up this declines rather than queueing behind intake.
        allowed = False
        try:
            from utils.wiseoldman import limiter

            allowed = await limiter.wait()
        except Exception:
            allowed = False

        def _fetch():
            from web_api.routes.sites import achievement_icon_url

            result = {"enabled": True, "period": period, "available": False}
            if allowed:
                quoted = urllib.parse.quote(name)
                try:
                    gains = shape_wom_gains(_wom_get(f"{quoted}/gained?period={period}"))
                    if gains is not None:
                        for boss in gains["bosses"]:
                            boss["name"] = boss["metric"].replace("_", " ").title()
                            boss["icon_url"] = achievement_icon_url(boss["metric"])
                        result.update(gains)
                        result["available"] = True
                except Exception:
                    pass
                # Achievements do not depend on the period: one fetch serves
                # all three, and outlives them in the cache.
                ach_key = f"wom:profile_achievements:{player_id}"
                conn = _rc()
                try:
                    raw = conn.get(ach_key) if conn is not None else None
                    if raw:
                        result["achievements"] = json.loads(raw)
                    elif result["available"]:
                        achievements = shape_wom_achievements(_wom_get(f"{quoted}/achievements"))
                        for a in achievements:
                            a["icon_url"] = achievement_icon_url(a["metric"])
                        result["achievements"] = achievements
                        if conn is not None:
                            conn.setex(ach_key, 2 * _WOM_HIT_TTL, json.dumps(achievements))
                except Exception:
                    pass
            conn = _rc()
            if conn is not None:
                try:
                    ttl = _WOM_HIT_TTL if result["available"] else _WOM_MISS_TTL
                    conn.setex(cache_key, ttl, json.dumps(result))
                except Exception:
                    pass
            return result

        payload = await asyncio.to_thread(_fetch)
    return with_cache_headers(jsonify(payload), max_age=600)
