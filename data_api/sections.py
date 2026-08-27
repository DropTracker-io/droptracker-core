"""The section registry: what ``?include=`` can ask for, and what it costs.

One registry drives both surfaces. ``/v2/players/<id>`` and
``/v2/groups/<id>/players`` call the *same* loaders — every loader takes a
list of player ids and returns ``{player_id: payload}``, so the single-player
route is simply the bulk route with one id. There is no second code path to
keep in sync, and no N+1: a loader issues one query for the whole page.

Each section declares a **cost weight**. A request's cost is
``len(player_ids) * sum(weight of each requested section)``, and that number
is what the rate limiter budgets against (``data_api.limits``). Weights are
ordered by how much work the data actually is:

    1  — a Redis GET that is already batched
    2  — one indexed row (or short PK range) per player
    3  — a bounded per-player set (~85 PBs, ~10 recent rows)
    5  — a rollup aggregation over a date range

Nothing here may touch ``drops``. The 207M-row table has exactly one safe
shape (``player_id`` + ``date_added`` range, index forced) and it does not
survive being run for a page of 100 players, so the loot sections read the
hourly rollups and Redis instead. That is the whole reason the rollups exist.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Dict, Iterable, List, NamedTuple

from sqlalchemy import text

#: Sections returned when the caller does not ask for any.
DEFAULT_SECTIONS = ("identity",)

#: Skill columns on ``player_exp``, in the game's own order.
SKILLS = (
    "attack", "strength", "defence", "ranged", "prayer", "magic", "runecraft",
    "hitpoints", "crafting", "mining", "smithing", "woodcutting", "farming",
    "firemaking", "fishing", "hunter", "herblore", "cooking", "thieving",
    "construction", "slayer", "agility", "fletching", "sailing",
)


class Section(NamedTuple):
    key: str
    cost: int
    #: ``(session, player_ids, ctx) -> {player_id: payload}``
    loader: Callable
    description: str


def _iso(value):
    """A timestamp column as ISO-8601, or None.

    Not every DATETIME arrives as a ``datetime``. MySQL's zero-date,
    ``'0000-00-00 00:00:00'``, is not a representable moment, so the driver
    hands back the raw string — 2,459 ``player_state`` rows carry exactly that.
    A bare ``.isoformat()`` therefore raises on real data. Treat the zero-date
    as what it means, "no timestamp", and pass any other string through rather
    than guessing at its format.
    """
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    text_value = str(value)
    return None if text_value.startswith("0000-00-00") else text_value


def _rows_by_player(rows) -> Dict[int, list]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[int(row[0])].append(row)
    return grouped


# ── loaders ──────────────────────────────────────────────────────────────────

def _load_identity(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    from utils.account_types import account_type_from_varbit

    rows = session.execute(text("""
        SELECT p.player_id, p.player_name, p.total_level, p.ehb, p.log_slots,
               p.date_added, p.account_type, s.account_type, s.combat_level,
               s.last_synced_at
        FROM players p
        LEFT JOIN player_state s ON s.player_id = p.player_id
        WHERE p.player_id IN :ids
    """).bindparams(ids=tuple(player_ids)))

    out = {}
    for r in rows:
        # The state-sync varbit is the live value; players.account_type is the
        # older wire-string path and only fills the gap before a first sync.
        account_type = account_type_from_varbit(r[7]) or r[6] or None
        out[int(r[0])] = {
            "player_id": int(r[0]),
            "name": r[1],
            "account_type": account_type,
            "combat_level": r[8],
            "total_level": r[2],
            "ehb": float(r[3]) if r[3] is not None else None,
            "first_seen": _iso(r[5]),
            "last_synced": _iso(r[9]),
        }
    return out


def _load_stats(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    columns = ", ".join(SKILLS)
    rows = session.execute(text(f"""
        SELECT player_id, {columns}, last_updated
        FROM player_exp WHERE player_id IN :ids
    """).bindparams(ids=tuple(player_ids)))

    out = {}
    for r in rows:
        skills = {name: int(r[i + 1] or 0) for i, name in enumerate(SKILLS)}
        out[int(r[0])] = {
            "skills": skills,
            "total_experience": sum(skills.values()),
            "last_updated": _iso(r[len(SKILLS) + 1]),
        }
    return out


def _load_clog(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    # The game's own counter (clog_slots/_total) is authoritative for progress;
    # our per-item rows can only ever be a subset of what it has seen.
    state = session.execute(text("""
        SELECT player_id, clog_slots, clog_slots_total
        FROM player_state WHERE player_id IN :ids
    """).bindparams(ids=tuple(player_ids)))
    out = {int(r[0]): {"obtained": r[1], "total": r[2], "tracked_items": 0}
           for r in state}

    counts = session.execute(text("""
        SELECT player_id, COUNT(*) FROM player_clog_items
        WHERE player_id IN :ids GROUP BY player_id
    """).bindparams(ids=tuple(player_ids)))
    for player_id, tracked in counts:
        out.setdefault(int(player_id), {"obtained": None, "total": None})
        out[int(player_id)]["tracked_items"] = int(tracked)
    return out


def _load_clog_slots(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    """Every recorded slot with its quantity — the expensive clog section."""
    rows = session.execute(text("""
        SELECT player_id, item_id, quantity FROM player_clog_items
        WHERE player_id IN :ids ORDER BY player_id, item_id
    """).bindparams(ids=tuple(player_ids)))

    out: Dict[int, dict] = {}
    for player_id, item_id, quantity in rows:
        entry = out.setdefault(int(player_id), {"slots": []})
        entry["slots"].append({"item_id": int(item_id), "quantity": int(quantity or 0)})
    return out


def _load_combat_achievements(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    # tasks_completed/points are denormalised precisely so a summary never has
    # to decode the raw varps; the full per-tier breakdown stays on the site.
    rows = session.execute(text("""
        SELECT player_id, tasks_completed, points, updated_at
        FROM player_ca_varps WHERE player_id IN :ids
    """).bindparams(ids=tuple(player_ids)))
    return {int(r[0]): {"tasks_completed": r[1], "points": r[2],
                        "updated_at": _iso(r[3])}
            for r in rows}


def _load_quests(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    rows = session.execute(text("""
        SELECT player_id, state, COUNT(*) FROM player_quest_states
        WHERE player_id IN :ids GROUP BY player_id, state
    """).bindparams(ids=tuple(player_ids)))

    out: Dict[int, dict] = {}
    labels = {0: "not_started", 1: "in_progress", 2: "finished"}
    for player_id, state, count in rows:
        entry = out.setdefault(int(player_id),
                               {"not_started": 0, "in_progress": 0, "finished": 0})
        label = labels.get(int(state))
        if label:
            entry[label] = int(count)
    return out


def _load_diaries(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    rows = session.execute(text("""
        SELECT player_id, area_id, tier, completed_count FROM player_diary_tiers
        WHERE player_id IN :ids ORDER BY player_id, area_id, tier
    """).bindparams(ids=tuple(player_ids)))

    out: Dict[int, dict] = {}
    for player_id, area_id, tier, completed in rows:
        entry = out.setdefault(int(player_id), {"areas": {}})
        entry["areas"].setdefault(str(int(area_id)), {})[str(int(tier))] = int(completed or 0)
    return out


def _load_personal_bests(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    # Bounded (~85 tracked bosses per player), so the whole set is safe to
    # return. MIN() collapses the per-attempt rows to the best per bracket.
    rows = session.execute(text("""
        SELECT player_id, npc_id, team_size, MIN(personal_best) AS best
        FROM personal_best
        WHERE player_id IN :ids AND personal_best IS NOT NULL AND personal_best > 0
        GROUP BY player_id, npc_id, team_size
        ORDER BY player_id, npc_id
    """).bindparams(ids=tuple(player_ids)))

    out: Dict[int, dict] = {}
    for player_id, npc_id, team_size, best in rows:
        entry = out.setdefault(int(player_id), {"bests": []})
        entry["bests"].append({
            "npc_id": int(npc_id),
            "team_size": team_size or "Solo",
            "best_ms": int(best),
        })
    return out


def _load_points(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    rows = session.execute(text("""
        SELECT player_id, COALESCE(SUM(amount), 0) FROM point_credits
        WHERE player_id IN :ids AND source NOT LIKE '%Upgrade%'
        GROUP BY player_id
    """).bindparams(ids=tuple(player_ids)))
    return {int(pid): {"lifetime_earned": int(total or 0)} for pid, total in rows}


def _load_badges(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    rows = session.execute(text("""
        SELECT pb.player_id, b.`key`, b.name, pb.group_id, pb.awarded_at
        FROM player_badges pb JOIN badges b ON b.badge_id = pb.badge_id
        WHERE pb.player_id IN :ids AND pb.status = 'active'
        ORDER BY pb.player_id, pb.awarded_at DESC
    """).bindparams(ids=tuple(player_ids)))

    out: Dict[int, dict] = {}
    for player_id, key, name, group_id, awarded_at in rows:
        entry = out.setdefault(int(player_id), {"badges": []})
        entry["badges"].append({
            "key": key, "name": name, "group_id": group_id,
            "awarded_at": _iso(awarded_at),
        })
    return out


def _load_pets(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    rows = session.execute(text("""
        SELECT player_id, item_id, pet_name, date_added FROM player_pets
        WHERE player_id IN :ids ORDER BY player_id, date_added
    """).bindparams(ids=tuple(player_ids)))

    out: Dict[int, dict] = {}
    for player_id, item_id, pet_name, date_added in rows:
        entry = out.setdefault(int(player_id), {"pets": []})
        entry["pets"].append({
            "item_id": int(item_id) if item_id is not None else None,
            "name": pet_name,
            "received": _iso(date_added),
        })
    return out


def _load_deaths(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    rows = session.execute(text("""
        SELECT player_id, COUNT(*), MAX(date_added) FROM player_deaths
        WHERE player_id IN :ids GROUP BY player_id
    """).bindparams(ids=tuple(player_ids)))
    return {int(pid): {"count": int(count), "last": _iso(last)}
            for pid, count, last in rows}


def _load_loot(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    """Headline GP from Redis — the same keys the leaderboard reads.

    Deliberately not recomputed from ``drops``: the leaderboard and this API
    must never disagree, and Redis already holds the answer batched.
    """
    from web_api.common import player_month_totals
    from utils.partitions import get_current_partition

    partition = ctx.get("partition") or get_current_partition()
    month = player_month_totals(player_ids, partition)

    all_time = {}
    try:
        from utils.redis import RedisClient

        conn = RedisClient().client
        pipe = conn.pipeline()
        for pid in player_ids:
            pipe.get(f"player:{pid}:all:total_loot")
        for pid, raw in zip(player_ids, pipe.execute()):
            all_time[pid] = int(raw) if raw else 0
    except Exception:
        all_time = {pid: None for pid in player_ids}

    return {int(pid): {"partition": partition,
                       "month_gp": int(month.get(pid, 0) or 0),
                       "all_time_gp": all_time.get(pid)}
            for pid in player_ids}


def _load_loot_npcs(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    """Per-NPC loot from the hourly rollup over a date range.

    Ranges on ``date_hour`` (a zero-padded 'YYYY-MM-DD-HH' string, so
    lexicographic order is chronological) under ``idx_player_npc_date_hour``.
    Never an equality on ``partition``, which degrades to a player-only scan.
    """
    start, end = ctx["date_hour_range"]
    limit = ctx.get("per_player_limit", 10)

    rows = session.execute(text("""
        SELECT player_id, npc_id, SUM(total_value) AS gp, SUM(drop_count) AS drops
        FROM player_npc_hourly_totals
        WHERE player_id IN :ids AND date_hour BETWEEN :start AND :end
        GROUP BY player_id, npc_id
        ORDER BY player_id, gp DESC
    """).bindparams(ids=tuple(player_ids), start=start, end=end))

    out: Dict[int, dict] = {}
    for player_id, npc_id, gp, drop_count in rows:
        entry = out.setdefault(int(player_id), {"from": start, "to": end, "npcs": []})
        if len(entry["npcs"]) < limit:
            entry["npcs"].append({"npc_id": int(npc_id), "gp": int(gp or 0),
                                  "drops": int(drop_count or 0)})
    return out


def _load_loot_items(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    start, end = ctx["date_hour_range"]
    limit = ctx.get("per_player_limit", 10)

    rows = session.execute(text("""
        SELECT player_id, item_id, SUM(total_value) AS gp,
               SUM(quantity) AS qty, SUM(drop_count) AS drops
        FROM player_item_hourly_totals
        WHERE player_id IN :ids AND date_hour BETWEEN :start AND :end
        GROUP BY player_id, item_id
        ORDER BY player_id, gp DESC
    """).bindparams(ids=tuple(player_ids), start=start, end=end))

    out: Dict[int, dict] = {}
    for player_id, item_id, gp, quantity, drop_count in rows:
        entry = out.setdefault(int(player_id), {"from": start, "to": end, "items": []})
        if len(entry["items"]) < limit:
            entry["items"].append({"item_id": int(item_id), "gp": int(gp or 0),
                                   "quantity": int(quantity or 0),
                                   "drops": int(drop_count or 0)})
    return out


# ── registry ─────────────────────────────────────────────────────────────────

_SECTIONS = (
    Section("identity", 0, _load_identity,
            "Name, account type, combat/total level, EHB, last sync."),
    Section("stats", 2, _load_stats,
            "Experience in all 24 skills, and their total."),
    Section("clog", 2, _load_clog,
            "Collection log progress: obtained / total, and rows we track."),
    Section("clog_slots", 8, _load_clog_slots,
            "Every recorded collection log slot with its quantity (~1.5k rows/player)."),
    Section("combat_achievements", 2, _load_combat_achievements,
            "Combat achievement tasks completed and points."),
    Section("quests", 2, _load_quests,
            "Quest counts by state."),
    Section("diaries", 2, _load_diaries,
            "Achievement diary completion counts per area and tier."),
    Section("personal_bests", 3, _load_personal_bests,
            "Best time per boss and team-size bracket."),
    Section("points", 2, _load_points,
            "Lifetime points earned."),
    Section("badges", 2, _load_badges,
            "Currently held badges."),
    Section("pets", 2, _load_pets,
            "Pets received."),
    Section("deaths", 2, _load_deaths,
            "Recorded death count and most recent."),
    Section("loot", 1, _load_loot,
            "Headline loot GP this month and all time (matches the leaderboard)."),
    Section("loot_npcs", 5, _load_loot_npcs,
            "Top NPCs by loot value over the requested window."),
    Section("loot_items", 5, _load_loot_items,
            "Top items by loot value over the requested window."),
)

REGISTRY: Dict[str, Section] = {s.key: s for s in _SECTIONS}
ALL_SECTION_KEYS = tuple(s.key for s in _SECTIONS)


def parse_include(raw: str) -> List[str]:
    """``?include=`` → an ordered, de-duplicated list of known section keys.

    ``all`` expands to everything. Unknown names raise ``ValueError`` rather
    than being ignored: a caller who misspells a section should be told, not
    handed a response silently missing the data they asked for.
    """
    if not raw or not raw.strip():
        return list(DEFAULT_SECTIONS)

    requested, seen = [], set()
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name == "all":
            for key in ALL_SECTION_KEYS:
                if key not in seen:
                    seen.add(key)
                    requested.append(key)
            continue
        if name not in REGISTRY:
            raise ValueError(name)
        if name not in seen:
            seen.add(name)
            requested.append(name)

    for key in DEFAULT_SECTIONS:
        if key not in seen:
            requested.insert(0, key)
            seen.add(key)
    return requested


def cost_of(sections: Iterable[str], player_count: int) -> int:
    """What this request will be charged against the caller's budget."""
    per_player = sum(REGISTRY[key].cost for key in sections if key in REGISTRY)
    return max(1, per_player * max(1, player_count))


def load_sections(session, sections: Iterable[str], player_ids: List[int],
                  ctx: dict) -> Dict[int, dict]:
    """Run each loader once for the whole page and merge by player.

    A loader that raises takes down only its own section: the rest of the
    response is still useful, and the caller is told which part is missing
    rather than getting a 500 for the whole page.
    """
    merged: Dict[int, dict] = {pid: {} for pid in player_ids}
    for key in sections:
        section = REGISTRY.get(key)
        if section is None:
            continue
        try:
            produced = section.loader(session, player_ids, ctx)
        except Exception as exc:
            from data_api.core import is_statement_timeout

            if is_statement_timeout(exc):
                raise
            for pid in player_ids:
                merged[pid][key] = {"error": "unavailable"}
            continue
        for pid in player_ids:
            if key == "identity":
                merged[pid].update(produced.get(pid, {}))
            else:
                merged[pid][key] = produced.get(pid)
    return merged
