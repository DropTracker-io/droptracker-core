"""The section registry: what ``?include=`` can ask for, and what it costs.

One registry drives both surfaces. ``/v2/players/<id>`` and
``/v2/groups/<id>/players`` call the *same* loaders — every loader takes a
list of player ids and returns ``{player_id: payload}``, so the single-player
route is simply the bulk route with one id. There is no second code path to
keep in sync, and no N+1: a loader issues one query for the whole page.

Each section declares a **cost weight**. A request's cost is
``len(player_ids) * sum(weight of each requested section)``, and that number
is what the rate limiter budgets against (``data_api.limits``).

**The weights are measured, not guessed.** One unit is roughly 0.05 ms of
server work per player, taken from timing every loader over 100 real players
with warm caches. The first version of this table was ordered by intuition on
a 1-8 scale; the real spread turned out to be 475:1, which meant the two
sections that actually cost something were priced like the ones that cost
nothing. Re-measure (and re-scale) if a loader's query changes materially —
`scripts/` has no runner for this, it is a manual benchmark.

Production is roughly twice as slow as the box these were taken on, which the
tier budgets account for rather than the weights: the weights only need to be
right *relative to each other*.

Nothing here may touch ``drops``. The 207M-row table has exactly one safe
shape (``player_id`` + ``date_added`` range, index forced) and it does not
survive being run for a page of 100 players, so the loot sections read the
hourly rollups and Redis instead. That is the whole reason the rollups exist.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Dict, Iterable, List, NamedTuple

from sqlalchemy import text

logger = logging.getLogger("data_api.sections")

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
    """Every recorded slot — the largest payload this API produces.

    Returned as a sorted id array plus a *sparse* quantity map rather than one
    object per slot. 61% of slots have quantity 1, so repeating
    ``{"item_id": …, "quantity": 1}`` 300,000 times is mostly punctuation:
    measured over 400 players it is 11.0 MB against 3.8 MB for this shape, and
    the difference is memory and serialisation time on every such request.

    Deliberately *not* delta-encoded, which would save a further 1.2x on the
    wire but make every consumer write a decoder to read a list of item ids.
    Absent from ``quantities`` means 1.
    """
    rows = session.execute(text("""
        SELECT player_id, item_id, quantity FROM player_clog_items
        WHERE player_id IN :ids ORDER BY player_id, item_id
    """).bindparams(ids=tuple(player_ids)))

    out: Dict[int, dict] = {}
    for player_id, item_id, quantity in rows:
        entry = out.setdefault(int(player_id), {"items": [], "quantities": {}})
        entry["items"].append(int(item_id))
        quantity = int(quantity or 0)
        if quantity != 1:
            entry["quantities"][str(int(item_id))] = quantity
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
    #
    # ``npc_id IS NOT NULL`` is not decoration: 100 legacy rows (43 players,
    # all written May-Sep 2025, before the writers started resolving the NPC
    # before inserting) carry a time with no boss attached. A PB that cannot
    # name what was killed is not reportable, and `int(None)` in the loop
    # below used to raise -- which cost every player on the page their whole
    # personal_bests section, not just the one holding the bad row.
    rows = session.execute(text("""
        SELECT player_id, npc_id, team_size, MIN(personal_best) AS best
        FROM personal_best
        WHERE player_id IN :ids AND npc_id IS NOT NULL
          AND personal_best IS NOT NULL AND personal_best > 0
        GROUP BY player_id, npc_id, team_size
        ORDER BY player_id, npc_id
    """).bindparams(ids=tuple(player_ids)))

    out: Dict[int, dict] = {}
    for player_id, npc_id, team_size, best in rows:
        entry = out.setdefault(int(player_id), {"bests": []})
        if npc_id is None:
            # Belt and braces with the WHERE clause above: the loop must not
            # be the thing standing between a nullable column and a section
            # that fails for everyone.
            continue
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
    from web_api.common import get_current_partition, player_month_totals

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

    # The top-N is taken inside the query. Aggregating every NPC and trimming
    # in Python meant fetching ~5,600 rows to keep ~950 of them; ROW_NUMBER
    # discards the rest before they cross the wire (measured 144ms -> 82ms).
    rows = session.execute(text("""
        SELECT player_id, npc_id, gp, drops FROM (
            SELECT player_id, npc_id,
                   SUM(total_value) AS gp, SUM(drop_count) AS drops,
                   ROW_NUMBER() OVER (
                       PARTITION BY player_id ORDER BY SUM(total_value) DESC
                   ) AS rn
            FROM player_npc_hourly_totals
            WHERE player_id IN :ids AND date_hour BETWEEN :start AND :end
            GROUP BY player_id, npc_id
        ) ranked
        WHERE rn <= :limit
        ORDER BY player_id, gp DESC
    """).bindparams(ids=tuple(player_ids), start=start, end=end, limit=limit))

    out: Dict[int, dict] = {}
    for player_id, npc_id, gp, drop_count in rows:
        entry = out.setdefault(int(player_id), {"from": start, "to": end, "npcs": []})
        entry["npcs"].append({"npc_id": int(npc_id), "gp": int(gp or 0),
                              "drops": int(drop_count or 0)})
    return out


def _load_loot_items(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    start, end = ctx["date_hour_range"]
    limit = ctx.get("per_player_limit", 10)

    # Same pushdown as the NPC breakdown, and it matters far more here: the
    # item rollup is the biggest table this API reads, and trimming in Python
    # meant building 33,481 dicts to keep 977. End to end, 2,289ms -> ~700ms.
    rows = session.execute(text("""
        SELECT player_id, item_id, gp, qty, drops FROM (
            SELECT player_id, item_id,
                   SUM(total_value) AS gp, SUM(quantity) AS qty,
                   SUM(drop_count) AS drops,
                   ROW_NUMBER() OVER (
                       PARTITION BY player_id ORDER BY SUM(total_value) DESC
                   ) AS rn
            FROM player_item_hourly_totals
            WHERE player_id IN :ids AND date_hour BETWEEN :start AND :end
            GROUP BY player_id, item_id
        ) ranked
        WHERE rn <= :limit
        ORDER BY player_id, gp DESC
    """).bindparams(ids=tuple(player_ids), start=start, end=end, limit=limit))

    out: Dict[int, dict] = {}
    for player_id, item_id, gp, quantity, drop_count in rows:
        entry = out.setdefault(int(player_id), {"from": start, "to": end, "items": []})
        entry["items"].append({"item_id": int(item_id), "gp": int(gp or 0),
                               "quantity": int(quantity or 0),
                               "drops": int(drop_count or 0)})
    return out



def _load_meta(session, player_ids: List[int], ctx) -> Dict[int, dict]:
    """Where each player sits on the boards, and which groups they are in.

    Free (cost 0) because it is two pipelined Redis round trips for the whole
    page plus one indexed query — no per-player work. The ranks come from the
    same sorted sets the site's leaderboards render, so they cannot disagree
    with a player's profile page.
    """
    from data_api.group_meta import player_ranks
    from web_api.common import get_current_partition

    partition = ctx.get("partition") or get_current_partition()
    ranks = player_ranks(player_ids, partition)

    memberships: Dict[int, list] = {pid: [] for pid in player_ids}
    rows = session.execute(text("""
        SELECT a.player_id, g.group_id, g.group_name
        FROM user_group_association a
        JOIN groups g ON g.group_id = a.group_id
        WHERE a.player_id IN :ids AND a.group_id > 2
        ORDER BY a.player_id, g.group_id
    """).bindparams(ids=tuple(player_ids)))
    for player_id, group_id, name in rows:
        memberships[int(player_id)].append({"group_id": int(group_id), "name": name})

    return {pid: {"partition": partition, **ranks.get(pid, {}),
                  "groups": memberships.get(pid, [])}
            for pid in player_ids}


# ── registry ─────────────────────────────────────────────────────────────────

_SECTIONS = (
    Section("identity", 0, _load_identity,
            "Name, account type, combat/total level, EHB, last sync."),
    Section("stats", 1, _load_stats,
            "Experience in all 24 skills, and their total."),
    Section("clog", 8, _load_clog,
            "Collection log progress: obtained / total, and rows we track."),
    Section("clog_slots", 161, _load_clog_slots,
            "Every recorded collection log slot with its quantity (~1.5k rows/player)."),
    Section("combat_achievements", 1, _load_combat_achievements,
            "Combat achievement tasks completed and points."),
    Section("quests", 4, _load_quests,
            "Quest counts by state."),
    Section("diaries", 8, _load_diaries,
            "Achievement diary completion counts per area and tier."),
    Section("personal_bests", 8, _load_personal_bests,
            "Best time per boss and team-size bracket."),
    Section("points", 8, _load_points,
            "Lifetime points earned."),
    Section("badges", 1, _load_badges,
            "Currently held badges."),
    Section("pets", 3, _load_pets,
            "Pets received."),
    Section("deaths", 3, _load_deaths,
            "Recorded death count and most recent."),
    Section("meta", 0, _load_meta,
            "Board standing (monthly and all-time rank) and group memberships. "
            "Free: two pipelined Redis round trips for the whole page. On a "
            "group request it also attaches the group's own stats."),
    Section("loot", 1, _load_loot,
            "Headline loot GP this month and all time (matches the leaderboard)."),
    Section("loot_npcs", 16, _load_loot_npcs,
            "Top NPCs by loot value over the requested window."),
    Section("loot_items", 121, _load_loot_items,
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


def _run_loader(session, section: Section, player_ids: List[int],
                ctx: dict) -> tuple:
    """``(produced, failed_ids)`` — a section's data, minus whoever broke it.

    A loader reads the whole page in one query, so anything that raises while
    walking the result set aborts it for *every* player on that page. One
    legacy ``personal_best`` row with a NULL ``npc_id`` therefore returned
    ``{"error": "unavailable"}`` for all 100 members of a roster, and the
    section looked broken for clans where 99 players were perfectly fine.

    So a page failure is not the answer, it is the prompt for a second
    question: re-run the loader one player at a time and let only the players
    who actually fail carry the error. Bounded by the page ceiling and only
    ever reached once something is already wrong, which is the moment to
    spend a few extra indexed lookups rather than throw good data away.

    A statement timeout is re-raised either way: that is the server saying the
    work is too heavy, and retrying it per player is exactly the wrong move.
    """
    from data_api.core import is_statement_timeout

    try:
        return section.loader(session, player_ids, ctx), []
    except Exception as exc:
        if is_statement_timeout(exc):
            raise
        logger.exception(
            "data_api section %r failed for %d player(s); retrying per player",
            section.key, len(player_ids))

    if len(player_ids) == 1:
        return {}, list(player_ids)

    # A DBAPI error leaves the connection needing one before it is reusable;
    # a plain TypeError in the row loop does not, and rolling back a
    # read-only session costs nothing.
    try:
        session.rollback()
    except Exception:
        pass

    produced: Dict[int, dict] = {}
    failed: List[int] = []
    for player_id in player_ids:
        try:
            produced.update(section.loader(session, [player_id], ctx))
        except Exception as exc:
            if is_statement_timeout(exc):
                raise
            logger.error("data_api section %r is unavailable for player %s: %s",
                         section.key, player_id, exc)
            failed.append(player_id)
            try:
                session.rollback()
            except Exception:
                pass
    return produced, failed


def load_sections(session, sections: Iterable[str], player_ids: List[int],
                  ctx: dict) -> Dict[int, dict]:
    """Run each loader once for the whole page and merge by player.

    A loader that raises takes down only its own section, and within that
    section only the players it actually cannot serve (see :func:`_run_loader`):
    the rest of the response is still useful, and the caller is told which
    part is missing rather than getting a 500 for the whole page.
    """
    merged: Dict[int, dict] = {pid: {} for pid in player_ids}
    for key in sections:
        section = REGISTRY.get(key)
        if section is None:
            continue
        produced, failed = _run_loader(session, section, player_ids, ctx)
        failed_ids = set(failed)
        for pid in player_ids:
            if pid in failed_ids:
                merged[pid][key] = {"error": "unavailable"}
            elif key == "identity":
                merged[pid].update(produced.get(pid, {}))
            else:
                merged[pid][key] = produced.get(pid)
    return merged
