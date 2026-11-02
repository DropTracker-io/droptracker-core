"""Clan stats: everything a clan did over a period, per member (suggestion #224).

One payload backs the website's ``/groups/{id}/stats`` page, the page-builder
block, the ``/clan-stats`` Discord command and the recap card's standouts. It
answers "what happened in this clan" at whatever depth the reader wants: clan
totals, a row per member, XP per skill, kill counts per boss, loot per source
and item, and the individual events (pets, log slots, PBs, CAs, quests,
diaries, deaths, slayer tasks, PvP kills, big drops).

Periods are ``'YYYY-MM'`` (a month), ``'YYYY'`` (a year) or ``'all'``.

Where each number comes from:

* **Loot** — Redis member totals, the same numbers as the leaderboards
  (``player:{id}:{YYYYMM}:total_loot``, ``leaderboard:all``).
* **Items, sources, activity** — the hourly rollups, ranged on ``date_hour``
  (see the ``services/recap`` notes: never ``partition =``).
* **XP, levels, boss KC, EHB, EHP, clues** — Wise Old Man. A closed month comes
  from ``recap_wom_gains.metrics``; the month in progress is read live (cached
  an hour); the all-time view uses current hiscores. All via
  ``services.recap_ehb``.
* **Achievements and events** — our own event tables, roster ``IN`` + date
  range, each indexed on ``player_id``.

The compute here is synchronous and reads only what is already stored. The
WOM reads that may touch the network happen in :func:`prepare_wom`, which the
async callers await first.

Omit, never zero: a source we have no data for is ``None`` in ``totals`` and
reported in ``coverage``, so readers can hide it instead of printing 0.
"""
from __future__ import annotations

import json
import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import bindparam, text

logger = logging.getLogger("services.group_stats")

PERIOD_ALL = "all"
# Our first month of tracked loot (Redis member totals persist back to here).
DATA_SINCE = "2024-10"
CACHE_PREFIX = "gstats:v1:"
# How many rows of each list the payload carries. Generous: the page filters
# and sorts client-side, and a member drilldown is read out of the same lists.
ITEMS_LIMIT = 60
MEMBER_ITEMS_LIMIT = 12
SOURCES_LIMIT = 60
FEED_LIMIT = 150
BIG_DROPS_LIMIT = 30
STANDOUT_DEPTH = 3
# Server-side cap on any one heavy read, seconds (MariaDB max_statement_time).
STATEMENT_SECONDS = 20

# --------------------------------------------------------------------------- #
# Wise Old Man metric vocabulary
# --------------------------------------------------------------------------- #
SKILLS = (
    "attack", "defence", "strength", "hitpoints", "ranged", "prayer", "magic",
    "cooking", "woodcutting", "fletching", "fishing", "firemaking", "crafting",
    "smithing", "mining", "herblore", "agility", "thieving", "slayer",
    "farming", "runecrafting", "hunter", "construction", "sailing",
)
OVERALL = "overall"
COMPUTED = ("ehp", "ehb")
CLUE_METRIC = "clue_scrolls_all"
# Everything WOM tracks that is neither a skill nor a boss. Anything not in
# SKILLS, COMPUTED or here is treated as a boss, so a boss WOM adds tomorrow
# shows up without a code change.
ACTIVITIES = frozenset({
    "league_points", "bounty_hunter_hunter", "bounty_hunter_rogue",
    "bounty_hunter_legacy_hunter", "bounty_hunter_legacy_rogue",
    "clue_scrolls_all", "clue_scrolls_beginner", "clue_scrolls_easy",
    "clue_scrolls_medium", "clue_scrolls_hard", "clue_scrolls_elite",
    "clue_scrolls_master", "last_man_standing", "pvp_arena", "soul_wars_zeal",
    "guardians_of_the_rift", "colosseum_glory", "collections_logged",
})


def metric_kind(metric: str) -> str:
    if metric == OVERALL or metric in SKILLS:
        return "skill"
    if metric in COMPUTED:
        return "computed"
    if metric in ACTIVITIES:
        return "activity"
    return "boss"


# OSRS experience table: XP needed for levels 1..126 (virtual levels beyond 99
# so a 200M skill doesn't read as "99, no change" forever).
def _build_xp_table() -> list[int]:
    points = 0
    table = [0, 0]  # index = level; level 1 needs 0
    for level in range(1, 126):
        points += int(level + 300 * 2 ** (level / 7.0))
        table.append(points // 4)
    return table


_XP_TABLE = _build_xp_table()


def xp_to_level(xp, virtual: bool = False) -> int:
    """The level a skill is at with ``xp`` experience (1-99, or up to 126)."""
    try:
        xp = int(xp)
    except (TypeError, ValueError):
        return 1
    if xp <= 0:
        return 1
    level = bisect_right(_XP_TABLE, xp) - 1
    level = max(1, level)
    return min(level, 126 if virtual else 99)


def levels_gained(start, end) -> int:
    """Non-virtual levels gained between two XP values. ``start`` of -1 (or
    None) is WOM's "unranked at the start", where the gain can't be known."""
    try:
        start, end = int(start), int(end)
    except (TypeError, ValueError):
        return 0
    if start < 0 or end <= start:
        return 0
    return max(0, xp_to_level(end) - xp_to_level(start))


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #
@dataclass
class Window:
    period: str
    kind: str  # "month" | "year" | "all"
    months: list[str] = field(default_factory=list)
    start: Optional[str] = None  # half-open datetime bounds; None for all
    end: Optional[str] = None
    hour_lo: Optional[str] = None  # inclusive date_hour bounds
    hour_hi: Optional[str] = None
    in_progress: bool = False


def current_month(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return f"{now.year:04d}-{now.month:02d}"


def parse_period(period: Optional[str], now: Optional[datetime] = None) -> Optional[Window]:
    """A :class:`Window`, or None for a period we don't serve (the future, or
    before tracking began)."""
    from services.recap import (
        is_month_period,
        is_year_period,
        month_bounds,
        month_hour_bounds,
        year_months,
    )

    now = now or datetime.now()
    this_month = current_month(now)
    period = (period or this_month).strip().lower()
    if period == PERIOD_ALL:
        return Window(period=PERIOD_ALL, kind="all", in_progress=True)
    if is_month_period(period):
        try:
            start, end = month_bounds(period)
            lo, hi = month_hour_bounds(period)
        except (ValueError, IndexError):
            return None
        if period > this_month or period < DATA_SINCE:
            return None
        return Window(period=period, kind="month", months=[period], start=start,
                      end=end, hour_lo=lo, hour_hi=hi,
                      in_progress=period == this_month)
    if is_year_period(period):
        year = int(period)
        if year > now.year or year < int(DATA_SINCE[:4]):
            return None
        months = [m for m in year_months(year) if DATA_SINCE <= m <= this_month]
        if not months:
            return None
        return Window(
            period=period, kind="year", months=months,
            start=f"{year:04d}-01-01 00:00:00", end=f"{year + 1:04d}-01-01 00:00:00",
            hour_lo=f"{year:04d}-01-01-00", hour_hi=f"{year:04d}-12-31-23",
            in_progress=year == now.year,
        )
    return None


def cache_ttl(window: Window) -> int:
    if window.kind == "all":
        return 1800
    if window.in_progress:
        return 600
    return 24 * 3600


# --------------------------------------------------------------------------- #
# WOM inputs (async; awaited before the sync compute)
# --------------------------------------------------------------------------- #
@dataclass
class WomInputs:
    """What Wise Old Man could tell us for this window.

    ``gains``: ``{player_id: {metric: [gained, start, end]}}`` (month/year).
    ``hiscores``: ``{player_id: {...}}`` current totals (all-time view).
    ``status``: "stored" | "live" | "partial" | "missing" | "unlinked".
    """
    gains: dict = field(default_factory=dict)
    hiscores: dict = field(default_factory=dict)
    status: str = "missing"
    months_covered: list[str] = field(default_factory=list)


async def prepare_wom(session, group_id: int, window: Window, player_ids: list[int]) -> WomInputs:
    """Gather the WOM side, harvesting a missing closed month on the way.

    Never raises; a WOM outage leaves the XP/KC columns empty and the rest of
    the page intact.
    """
    from services import recap_ehb

    out = WomInputs()
    try:
        wom_id = recap_ehb._group_wom_id(session, group_id)
        if window.kind == "all":
            if not wom_id:
                out.status = "unlinked"
                return out
            hiscores = await recap_ehb.group_hiscores(group_id)
            if hiscores is not None:
                out.hiscores = hiscores
                out.status = "live"
            return out

        closed = [m for m in window.months if not (window.in_progress and m == window.months[-1])]
        have = recap_ehb.harvested_periods(session, player_ids, closed)
        # A month with no full-metric rows yet is fetched once, on demand. Only
        # a few per request: a year view of an un-harvested clan would
        # otherwise be twelve calls in one page load.
        if wom_id:
            for month in [m for m in closed if m not in have][:3]:
                await recap_ehb.ensure_group_month(group_id, month)
            have = recap_ehb.harvested_periods(session, player_ids, closed)
        gains = recap_ehb.stored_metrics(session, player_ids, sorted(have)) if have else {}
        covered = sorted(have)

        if window.in_progress and wom_id:
            live = await recap_ehb.live_group_gains(group_id, window.months[-1])
            if live is not None:
                covered.append(window.months[-1])
                for pid, metrics in live.items():
                    merged = gains.setdefault(int(pid), {})
                    for metric, values in metrics.items():
                        held = merged.get(metric)
                        if held is None:
                            merged[metric] = list(values)
                        else:
                            held[0] = (held[0] or 0) + (values[0] or 0)
                            held[2] = values[2]
        out.gains = gains
        out.months_covered = covered
        if not wom_id and not covered:
            out.status = "unlinked"
        elif not covered:
            out.status = "missing"
        elif len(covered) == len(window.months):
            out.status = "live" if window.in_progress else "stored"
        else:
            out.status = "partial"
    except Exception as e:
        logger.warning("clan stats WOM inputs failed for group %s %s: %s",
                       group_id, window.period, e, exc_info=True)
    return out


# --------------------------------------------------------------------------- #
# SQL helpers
# --------------------------------------------------------------------------- #
def _capped(sql: str) -> str:
    return f"SET STATEMENT max_statement_time={STATEMENT_SECONDS} FOR {sql}"


def _exec(session, sql: str, params: dict, *expanding: str, capped: bool = False):
    stmt = text(_capped(sql) if capped else sql)
    if expanding:
        stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))
    return session.execute(stmt, params).fetchall()


def _date_clause(window: Window, col: str = "date_added") -> str:
    if window.kind == "all":
        return ""
    return f" AND {col} >= :start AND {col} < :end "


def _window_params(window: Window, **extra) -> dict:
    params = dict(extra)
    if window.kind != "all":
        params["start"] = window.start
        params["end"] = window.end
    return params


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat(timespec="seconds")
    except Exception:
        return str(value)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _loot_totals(player_ids: list[int], window: Window) -> dict[int, int]:
    from web_api.common import player_month_totals

    if window.kind == "all":
        return {k: int(v) for k, v in player_month_totals(player_ids, "all").items()}
    out: dict[int, int] = {}
    for month in window.months:
        partition = int(month.replace("-", ""))
        for pid, gp in player_month_totals(player_ids, partition).items():
            if gp:
                out[pid] = out.get(pid, 0) + int(gp)
    return out


def _item_rows(session, player_ids: list[int], window: Window) -> list:
    if window.kind == "all":
        return []
    return _exec(
        session,
        "SELECT r.item_id, i.item_name, r.player_id, SUM(r.quantity), "
        "       SUM(r.total_value), SUM(r.drop_count) "
        "FROM player_item_hourly_totals r "
        "LEFT JOIN items i ON i.item_id = r.item_id "
        "WHERE r.player_id IN :pids AND r.date_hour BETWEEN :lo AND :hi "
        "GROUP BY r.item_id, i.item_name, r.player_id",
        {"pids": player_ids, "lo": window.hour_lo, "hi": window.hour_hi},
        "pids", capped=True,
    )


def _npc_rows(session, player_ids: list[int], window: Window) -> list:
    if window.kind == "all":
        return []
    return _exec(
        session,
        "SELECT r.npc_id, n.npc_name, r.player_id, SUM(r.total_value), "
        "       SUM(r.drop_count) "
        "FROM player_npc_hourly_totals r "
        "LEFT JOIN npc_list n ON n.npc_id = r.npc_id "
        "WHERE r.player_id IN :pids AND r.date_hour BETWEEN :lo AND :hi "
        "GROUP BY r.npc_id, n.npc_name, r.player_id",
        {"pids": player_ids, "lo": window.hour_lo, "hi": window.hour_hi},
        "pids", capped=True,
    )


def _npc_coverage(session, window: Window) -> bool:
    """Whether the NPC rollup has rows for every month of the window (it has a
    202509-202606 hole; see services/recap._npc_coverage)."""
    if window.kind == "all":
        return False
    from services.recap import _npc_coverage as month_covered

    try:
        return all(month_covered(session, m) for m in window.months)
    except Exception:
        return False


def _activity(session, player_ids: list[int], window: Window) -> Optional[dict]:
    if window.kind == "all":
        return None
    rows = _exec(
        session,
        "SELECT RIGHT(date_hour, 2) AS hh, "
        "       DAYOFWEEK(STR_TO_DATE(LEFT(date_hour, 10), '%Y-%m-%d')) AS dw, "
        "       COUNT(DISTINCT player_id, date_hour) "
        "FROM player_item_hourly_totals "
        "WHERE player_id IN :pids AND date_hour BETWEEN :lo AND :hi "
        "GROUP BY hh, dw",
        {"pids": player_ids, "lo": window.hour_lo, "hi": window.hour_hi},
        "pids", capped=True,
    )
    by_hour = [0] * 24
    by_weekday = [0] * 7
    for hh, dw, active in rows:
        try:
            by_hour[int(hh)] += int(active or 0)
            by_weekday[(int(dw) + 5) % 7] += int(active or 0)
        except (TypeError, ValueError, IndexError):
            continue
    return {"by_hour": by_hour, "by_weekday": by_weekday}


# (payload key, table, extra WHERE)
_COUNT_SOURCES = (
    ("pbs", "personal_best", ""),
    ("clog_slots", "collection", ""),
    ("cas", "combat_achievement", ""),
    ("pets", "player_pets", ""),
    ("quests", "quest_completions", ""),
    ("diaries", "diary_completions", ""),
    ("deaths", "player_deaths", ""),
    ("slayer_tasks", "slayer_task_completions", ""),
    ("pvp_kills", "pvp_kills",
     " AND kind = 'kill' AND hidden = 0 AND excluded_reason IS NULL"),
)


def _counts(session, player_ids: list[int], window: Window) -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    for key, table, extra in _COUNT_SOURCES:
        try:
            rows = _exec(
                session,
                f"SELECT player_id, COUNT(*) FROM {table} "
                f"WHERE player_id IN :pids{_date_clause(window)}{extra} "
                f"GROUP BY player_id",
                _window_params(window, pids=player_ids),
                "pids", capped=True,
            )
            out[key] = {int(p): int(c) for p, c in rows if p is not None and c}
        except Exception as e:
            # A table missing on some deployment must not take the page down.
            logger.debug("clan stats count %s failed: %s", table, e)
            session.rollback()
            out[key] = {}
    return out


def _pvp_loot(session, player_ids: list[int], window: Window) -> dict[int, int]:
    try:
        rows = _exec(
            session,
            "SELECT player_id, SUM(total_value) FROM pvp_kills "
            f"WHERE player_id IN :pids{_date_clause(window)} "
            "  AND hidden = 0 AND excluded_reason IS NULL GROUP BY player_id",
            _window_params(window, pids=player_ids), "pids", capped=True,
        )
        return {int(p): int(v or 0) for p, v in rows if p is not None and v}
    except Exception:
        session.rollback()
        return {}


# Each feed: (key, SQL selecting player_id, date_added, then detail columns)
_FEEDS = {
    "pets": (
        "SELECT t.player_id, t.date_added, t.item_id, COALESCE(t.pet_name, i.item_name) "
        "FROM player_pets t LEFT JOIN items i ON i.item_id = t.item_id "
        "WHERE t.player_id IN :pids{dates} ORDER BY t.date_added DESC LIMIT {limit}",
        ("item_id", "name"),
    ),
    "clog": (
        "SELECT t.player_id, t.date_added, t.item_id, i.item_name, n.npc_name, t.reported_slots "
        "FROM collection t LEFT JOIN items i ON i.item_id = t.item_id "
        "LEFT JOIN npc_list n ON n.npc_id = t.npc_id "
        "WHERE t.player_id IN :pids{dates} ORDER BY t.date_added DESC LIMIT {limit}",
        ("item_id", "name", "source", "slots"),
    ),
    "pbs": (
        "SELECT t.player_id, t.date_added, t.npc_id, n.npc_name, t.personal_best, "
        "       t.team_size, t.precise_timing "
        "FROM personal_best t LEFT JOIN npc_list n ON n.npc_id = t.npc_id "
        "WHERE t.player_id IN :pids{dates} ORDER BY t.date_added DESC LIMIT {limit}",
        ("npc_id", "name", "time_ms", "team_size", "precise"),
    ),
    "cas": (
        "SELECT t.player_id, t.date_added, t.task_name FROM combat_achievement t "
        "WHERE t.player_id IN :pids{dates} ORDER BY t.date_added DESC LIMIT {limit}",
        ("name",),
    ),
    "quests": (
        "SELECT t.player_id, t.date_added, t.quest_name, t.quest_points FROM quest_completions t "
        "WHERE t.player_id IN :pids{dates} ORDER BY t.date_added DESC LIMIT {limit}",
        ("name", "quest_points"),
    ),
    "diaries": (
        "SELECT t.player_id, t.date_added, t.diary_name, t.diary_tier FROM diary_completions t "
        "WHERE t.player_id IN :pids{dates} ORDER BY t.date_added DESC LIMIT {limit}",
        ("name", "tier"),
    ),
    "deaths": (
        "SELECT t.player_id, t.date_added, t.source, COALESCE(t.region_name, t.location), "
        "       t.is_pvp, t.value_lost FROM player_deaths t "
        "WHERE t.player_id IN :pids{dates} ORDER BY t.date_added DESC LIMIT {limit}",
        ("killer", "location", "pvp", "value_lost"),
    ),
    "slayer": (
        "SELECT t.player_id, t.date_added, t.task_name, t.amount_initial, t.master_name, "
        "       t.points_awarded FROM slayer_task_completions t "
        "WHERE t.player_id IN :pids{dates} ORDER BY t.date_added DESC LIMIT {limit}",
        ("name", "amount", "master", "points"),
    ),
    "pvp": (
        "SELECT t.player_id, t.date_added, t.victim_name, t.total_value, t.location, "
        "       t.wilderness_level FROM pvp_kills t "
        "WHERE t.player_id IN :pids{dates} AND t.kind = 'kill' AND t.hidden = 0 "
        "  AND t.excluded_reason IS NULL ORDER BY t.date_added DESC LIMIT {limit}",
        ("victim", "value", "location", "wilderness"),
    ),
}


def _feeds(session, player_ids: list[int], window: Window) -> dict[str, list]:
    out: dict[str, list] = {}
    for key, (sql, cols) in _FEEDS.items():
        try:
            rows = _exec(
                session,
                sql.format(dates=_date_clause(window, "t.date_added"), limit=FEED_LIMIT),
                _window_params(window, pids=player_ids), "pids", capped=True,
            )
        except Exception as e:
            logger.debug("clan stats feed %s failed: %s", key, e)
            session.rollback()
            out[key] = []
            continue
        entries = []
        for row in rows:
            entry = {"player_id": int(row[0]), "date": _iso(row[1])}
            for col, value in zip(cols, row[2:]):
                if isinstance(value, (bytes, bytearray)):
                    value = bool(int.from_bytes(value, "little"))
                entry[col] = value
            if "pvp" in entry and entry["pvp"] is not None:
                entry["pvp"] = bool(entry["pvp"])
            if "precise" in entry and entry["precise"] is not None:
                entry["precise"] = bool(entry["precise"])
            entries.append(entry)
        out[key] = entries
    return out


def _big_drops(session, player_ids: list[int], window: Window) -> list[dict]:
    """The window's largest single drops, with proof screenshots.

    A straight ``ORDER BY value`` over the roster's ``drops`` reads every drop
    the clan had (measured 15s for a 500-member month). Instead the hourly
    rollup names the candidates: cells ranked by value per drop, so a cell that
    holds one big drop ranks above fifty rune stacks. Each candidate is then
    one indexed lookup on ``(player_id, date_added)`` over a two-hour slice.
    """
    if window.kind == "all":
        return []
    from datetime import timedelta

    from services.recap import _public_image_url

    try:
        cells = _exec(
            session,
            "SELECT player_id, item_id, date_hour FROM player_item_hourly_totals "
            "WHERE player_id IN :pids AND date_hour BETWEEN :lo AND :hi "
            "ORDER BY total_value / GREATEST(drop_count, 1) DESC LIMIT :lim",
            {"pids": player_ids, "lo": window.hour_lo, "hi": window.hour_hi,
             "lim": BIG_DROPS_LIMIT * 2},
            "pids", capped=True,
        )
    except Exception as e:
        logger.debug("clan stats big drop candidates failed: %s", e)
        session.rollback()
        return []

    found: dict[int, dict] = {}
    for pid, item_id, date_hour in cells:
        try:
            hour = datetime.strptime(str(date_hour), "%Y-%m-%d-%H")
        except ValueError:
            continue
        # One hour either side: the rollup buckets by the row's own time, and
        # a row stamped at 10:59:59.9 must not slip out of an 11:00 cell.
        lo = (hour - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        hi = (hour + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            rows = _exec(
                session,
                "SELECT d.drop_id, d.player_id, d.item_id, i.item_name, d.npc_id, "
                "       n.npc_name, d.value * d.quantity AS total_value, d.quantity, "
                "       d.date_added, d.image_url "
                "FROM drops d FORCE INDEX (ix_drops_player_id_date_added) "
                "LEFT JOIN items i ON i.item_id = d.item_id "
                "LEFT JOIN npc_list n ON n.npc_id = d.npc_id "
                "WHERE d.player_id = :pid AND d.date_added >= :lo "
                "  AND d.date_added < :hi AND d.item_id = :iid AND d.hidden = 0 "
                "ORDER BY total_value DESC LIMIT 2",
                {"pid": int(pid), "iid": int(item_id), "lo": lo, "hi": hi},
            )
        except Exception as e:
            logger.debug("clan stats big drop lookup failed: %s", e)
            session.rollback()
            continue
        for (drop_id, row_pid, row_item, item_name, npc_id, npc_name, total, qty,
             date_added, image_url) in rows:
            if window.start and date_added is not None:
                stamp = date_added.strftime("%Y-%m-%d %H:%M:%S")
                if stamp < window.start or stamp >= window.end:
                    continue
            found[int(drop_id)] = {
                "drop_id": int(drop_id), "player_id": int(row_pid),
                "item_id": int(row_item) if row_item else None, "name": item_name,
                "npc_id": int(npc_id) if npc_id else None, "source": npc_name,
                "value": int(total or 0), "quantity": int(qty or 0),
                "date": _iso(date_added), "image_url": _public_image_url(image_url),
            }
    out = sorted(found.values(), key=lambda d: d["value"], reverse=True)
    return out[:BIG_DROPS_LIMIT]


def _names(session, player_ids: Iterable[int]) -> dict[int, str]:
    from services.recap import _player_names

    return _player_names(session, sorted(set(player_ids)))


# --------------------------------------------------------------------------- #
# Folding (pure: unit-tested without a database)
# --------------------------------------------------------------------------- #
MEMBER_FIELDS = (
    "loot", "drops", "xp", "levels", "ehb", "ehp", "kills", "clues", "pbs",
    "clog_slots", "cas", "pets", "quests", "diaries", "deaths", "slayer_tasks",
    "pvp_kills", "pvp_loot",
)


def fold_gains(gains: dict) -> tuple[dict, dict, dict]:
    """Per-member WOM totals plus the per-skill and per-boss breakdowns.

    Returns ``(member_totals, skills, bosses)``:

    * ``member_totals[pid]`` -> ``{xp, levels, ehb, ehp, kills, clues}``
    * ``skills[metric]`` -> ``[[pid, xp, levels], ...]``
    * ``bosses[metric]`` -> ``[[pid, kills], ...]``
    """
    members: dict[int, dict] = {}
    skills: dict[str, list] = {}
    bosses: dict[str, list] = {}
    for pid, metrics in (gains or {}).items():
        pid = int(pid)
        row = {"xp": 0, "levels": 0, "ehb": 0.0, "ehp": 0.0, "kills": 0, "clues": 0}
        for metric, values in (metrics or {}).items():
            try:
                gained, start, end = values
            except (TypeError, ValueError):
                continue
            if not gained or gained <= 0:
                continue
            kind = metric_kind(metric)
            if metric == OVERALL:
                row["xp"] = int(gained)
            elif kind == "skill":
                lv = levels_gained(start, end)
                row["levels"] += lv
                skills.setdefault(metric, []).append([pid, int(gained), lv])
            elif kind == "computed":
                row[metric] = round(float(gained), 2)
            elif kind == "boss":
                row["kills"] += int(gained)
                bosses.setdefault(metric, []).append([pid, int(gained)])
            elif metric == CLUE_METRIC:
                row["clues"] = int(gained)
        members[pid] = row
    for rows in skills.values():
        rows.sort(key=lambda r: r[1], reverse=True)
    for rows in bosses.values():
        rows.sort(key=lambda r: r[1], reverse=True)
    return members, skills, bosses


def fold_hiscores(hiscores: dict) -> tuple[dict, dict, dict]:
    """The all-time equivalents of :func:`fold_gains`: current totals.

    ``member_totals[pid]`` -> ``{xp, total_level, ehb, ehp, kills, clues}``;
    ``skills[metric]`` -> ``[[pid, xp, level], ...]``; ``bosses[metric]`` ->
    ``[[pid, kc], ...]``.
    """
    members: dict[int, dict] = {}
    skills: dict[str, list] = {}
    bosses: dict[str, list] = {}
    for pid, entry in (hiscores or {}).items():
        pid = int(pid)
        overall = (entry.get("skills") or {}).get(OVERALL) or [0, 0]
        row = {
            "xp": int(overall[0] or 0),
            "total_level": int(overall[1] or 0),
            "ehb": float(entry.get("ehb") or 0.0),
            "ehp": float(entry.get("ehp") or 0.0),
            "kills": 0,
            "clues": int((entry.get("activities") or {}).get(CLUE_METRIC) or 0),
        }
        for metric, values in (entry.get("skills") or {}).items():
            if metric == OVERALL:
                continue
            skills.setdefault(metric, []).append([pid, int(values[0]), int(values[1])])
        for metric, kc in (entry.get("bosses") or {}).items():
            row["kills"] += int(kc)
            bosses.setdefault(metric, []).append([pid, int(kc)])
        members[pid] = row
    for rows in skills.values():
        rows.sort(key=lambda r: r[1], reverse=True)
    for rows in bosses.values():
        rows.sort(key=lambda r: r[1], reverse=True)
    return members, skills, bosses


def fold_items(rows) -> tuple[list, dict, dict, dict]:
    """``(items, member_items, member_drops, member_rollup_loot)`` from
    ``(item_id, name, pid, qty, loot, drops)`` rollup rows."""
    merged: dict[int, dict] = {}
    member_items: dict[int, list] = {}
    member_drops: dict[int, int] = {}
    member_loot: dict[int, int] = {}
    for item_id, name, pid, qty, loot, drops in rows or ():
        if item_id is None or pid is None:
            continue
        item_id, pid = int(item_id), int(pid)
        qty, loot, drops = int(qty or 0), int(loot or 0), int(drops or 0)
        entry = merged.setdefault(item_id, {
            "item_id": item_id, "name": name or f"Item {item_id}",
            "quantity": 0, "loot": 0, "drops": 0, "receivers": [],
        })
        entry["quantity"] += qty
        entry["loot"] += loot
        entry["drops"] += drops
        entry["receivers"].append([pid, loot, qty])
        member_items.setdefault(pid, []).append([item_id, qty, loot])
        member_drops[pid] = member_drops.get(pid, 0) + drops
        member_loot[pid] = member_loot.get(pid, 0) + loot
    items = sorted(merged.values(), key=lambda x: x["loot"], reverse=True)
    for entry in items:
        entry["receivers"].sort(key=lambda r: r[1], reverse=True)
        entry["receiver_count"] = len(entry["receivers"])
        entry["receivers"] = entry["receivers"][:8]
    for pid, lst in member_items.items():
        lst.sort(key=lambda r: r[2], reverse=True)
        member_items[pid] = lst[:MEMBER_ITEMS_LIMIT]
    return items, member_items, member_drops, member_loot


def item_names_for(items: list, member_items: dict) -> dict:
    """Names for every item id a member drilldown can show."""
    names = {i["item_id"]: i["name"] for i in items}
    return {str(iid): names.get(iid) for lst in member_items.values() for iid, _q, _l in lst}


def fold_sources(rows) -> list:
    merged: dict[int, dict] = {}
    for npc_id, name, pid, loot, drops in rows or ():
        if npc_id is None or pid is None:
            continue
        entry = merged.setdefault(int(npc_id), {
            "npc_id": int(npc_id), "name": name or f"NPC {npc_id}",
            "loot": 0, "drops": 0, "members": [],
        })
        entry["loot"] += int(loot or 0)
        entry["drops"] += int(drops or 0)
        entry["members"].append([int(pid), int(loot or 0), int(drops or 0)])
    out = sorted(merged.values(), key=lambda x: x["loot"], reverse=True)
    for entry in out:
        entry["members"].sort(key=lambda r: r[1], reverse=True)
        entry["member_count"] = len(entry["members"])
        entry["members"] = entry["members"][:50]
    return out


# The fixed standout categories, in the order the page lists them. ``unit`` is
# how a reader formats the value. Shared with the recap designer
# (services/recap_design.STANDOUT_KEYS), so a key here is a key there.
STANDOUTS = (
    ("loot", "Most loot", "gp"),
    ("biggest_drop", "Biggest drop", "gp"),
    ("xp", "Most XP gained", "xp"),
    ("levels", "Most levels gained", "count"),
    ("ehb", "Most EHB gained", "hours"),
    ("ehp", "Most EHP gained", "hours"),
    ("kills", "Most boss kills", "count"),
    ("drops", "Most drops", "count"),
    ("clog_slots", "Most log slots", "count"),
    ("pets", "Most pets", "count"),
    ("pbs", "Most personal bests", "count"),
    ("cas", "Most combat achievements", "count"),
    ("clues", "Most clues", "count"),
    ("slayer_tasks", "Most slayer tasks", "count"),
    ("quests", "Most quests", "count"),
    ("diaries", "Most diaries", "count"),
    ("pvp_kills", "Most PvP kills", "count"),
    ("deaths", "Most deaths", "count"),
)
STANDOUT_KEYS = tuple(k for k, _, _ in STANDOUTS)


def build_standouts(members: list[dict], big_drops: list[dict], skills: dict,
                    bosses: dict, depth: int = STANDOUT_DEPTH) -> dict:
    """Top ``depth`` members per category, plus skill and boss leaders.

    Categories nobody scored in are absent rather than listing zeros.
    """
    categories = []
    for key, label, unit in STANDOUTS:
        if key == "biggest_drop":
            entries = [
                {"player_id": d["player_id"], "value": d["value"],
                 "detail": d.get("name"), "item_id": d.get("item_id")}
                for d in big_drops[:depth]
            ]
        else:
            ranked = sorted(
                (m for m in members if (m.get(key) or 0) > 0),
                key=lambda m: m.get(key) or 0, reverse=True,
            )
            entries = [{"player_id": m["player_id"], "value": m[key]} for m in ranked[:depth]]
        if entries:
            categories.append({"key": key, "label": label, "unit": unit, "entries": entries})

    skill_leaders = sorted(
        ({"metric": metric, "total": sum(r[1] for r in rows),
          "entries": [{"player_id": r[0], "value": r[1]} for r in rows[:depth]]}
         for metric, rows in skills.items() if rows),
        key=lambda s: s["total"], reverse=True,
    )
    boss_leaders = sorted(
        ({"metric": metric, "total": sum(r[1] for r in rows),
          "entries": [{"player_id": r[0], "value": r[1]} for r in rows[:depth]]}
         for metric, rows in bosses.items() if rows),
        key=lambda s: s["total"], reverse=True,
    )
    return {"categories": categories, "skills": skill_leaders, "bosses": boss_leaders}


def _sum_or_none(members: list[dict], key: str, available: bool):
    if not available:
        return None
    total = sum(m.get(key) or 0 for m in members)
    return round(total, 2) if isinstance(total, float) else total


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #
def member_table(session, player_ids: list[int], window: Window, wom: WomInputs,
                 *, with_items: bool = True) -> dict:
    """Everything except the group header and the feeds — the part the recap
    card reuses. Synchronous; reads only stored data plus ``wom``."""
    loot = _loot_totals(player_ids, window)
    counts = _counts(session, player_ids, window)
    pvp_loot = _pvp_loot(session, player_ids, window)
    item_rows = _item_rows(session, player_ids, window) if with_items else []
    items, member_items, member_drops, _ = fold_items(item_rows)

    if window.kind == "all":
        wom_members, skills, bosses = fold_hiscores(wom.hiscores)
    else:
        wom_members, skills, bosses = fold_gains(wom.gains)
    wom_available = bool(wom_members)

    members = []
    for pid in player_ids:
        row = {"player_id": pid, "loot": loot.get(pid, 0), "drops": member_drops.get(pid, 0)}
        row.update(wom_members.get(pid, {}))
        for key in ("pbs", "clog_slots", "cas", "pets", "quests", "diaries",
                    "deaths", "slayer_tasks", "pvp_kills"):
            row[key] = counts.get(key, {}).get(pid, 0)
        row["pvp_loot"] = pvp_loot.get(pid, 0)
        # Active = did anything we can see. Inactive members still get a row
        # (the roster view lists everyone) but sort to the bottom.
        row["active"] = any(
            (row.get(k) or 0) > 0 for k in MEMBER_FIELDS if k not in ("ehp",)
        )
        members.append(row)
    members.sort(key=lambda m: (m["loot"], m.get("xp") or 0), reverse=True)

    totals = {
        "members_total": len(player_ids),
        "members_active": sum(1 for m in members if m["active"]),
        "loot": sum(m["loot"] for m in members),
        "drops": sum(m["drops"] for m in members) if window.kind != "all" else None,
        "unique_items": len(items) if window.kind != "all" else None,
        "xp": _sum_or_none(members, "xp", wom_available),
        "levels": _sum_or_none(members, "levels", wom_available and window.kind != "all"),
        "ehb": _sum_or_none(members, "ehb", wom_available),
        "ehp": _sum_or_none(members, "ehp", wom_available),
        "kills": _sum_or_none(members, "kills", wom_available),
        "clues": _sum_or_none(members, "clues", wom_available),
        "pvp_loot": sum(m["pvp_loot"] for m in members),
    }
    for key in ("pbs", "clog_slots", "cas", "pets", "quests", "diaries", "deaths",
                "slayer_tasks", "pvp_kills"):
        totals[key] = sum(m[key] for m in members)

    return {
        "members": members,
        "totals": totals,
        "items": items[:ITEMS_LIMIT],
        "member_items": {str(k): v for k, v in member_items.items()},
        "item_names": item_names_for(items, member_items),
        "skills": skills,
        "bosses": bosses,
        "wom_available": wom_available,
    }


def compute(session, group_id: int, window: Window, wom: WomInputs) -> Optional[dict]:
    """The full clan stats payload, or None for an unknown group."""
    from services.recap import MAX_ROSTER_FOR_RECAP, _visible_group_player_ids

    group = session.execute(
        text("SELECT group_id, group_name, wom_id FROM groups WHERE group_id = :gid"),
        {"gid": int(group_id)},
    ).first()
    if not group or int(group_id) in (1, 2):
        return None
    player_ids = _visible_group_player_ids(session, group_id)
    if len(player_ids) > MAX_ROSTER_FOR_RECAP:
        return None

    table = member_table(session, player_ids, window, wom) if player_ids else {
        "members": [], "totals": {"members_total": 0, "members_active": 0},
        "items": [], "member_items": {}, "item_names": {}, "skills": {}, "bosses": {},
        "wom_available": False,
    }
    feeds = _feeds(session, player_ids, window) if player_ids else {}
    big_drops = _big_drops(session, player_ids, window) if player_ids else []
    npc_ok = bool(player_ids) and _npc_coverage(session, window)
    sources = fold_sources(_npc_rows(session, player_ids, window))[:SOURCES_LIMIT] if npc_ok else []
    activity = _activity(session, player_ids, window) if player_ids else None

    named = set(player_ids)
    names = _names(session, named)
    standouts = build_standouts(table["members"], big_drops, table["skills"], table["bosses"])

    # Every member's row, not a top N: the page's member drilldown reads each
    # member's skills and bosses back out of these lists. Rows are 2-3 ints, so
    # even a 500-member clan's full matrix stays well under 200 KB.
    skills = [
        {"metric": metric, "total": sum(r[1] for r in rows),
         "levels": sum(r[2] for r in rows) if window.kind != "all" else None,
         "members": rows, "member_count": len(rows)}
        for metric, rows in table["skills"].items()
    ]
    skills.sort(key=lambda s: s["total"], reverse=True)
    bosses = [
        {"metric": metric, "total": sum(r[1] for r in rows),
         "members": rows, "member_count": len(rows)}
        for metric, rows in table["bosses"].items()
    ]
    bosses.sort(key=lambda b: b["total"], reverse=True)

    return {
        "group": {"id": int(group[0]), "name": group[1], "wom_linked": bool(group[2])},
        "period": window.period,
        "kind": window.kind,
        "months": window.months,
        "in_progress": window.in_progress,
        "data_since": DATA_SINCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "coverage": {
            "wom": wom.status,
            "wom_months": wom.months_covered,
            "sources": npc_ok,
            "items": window.kind != "all",
        },
        "names": {str(k): v for k, v in names.items()},
        "totals": table["totals"],
        "members": table["members"],
        "standouts": standouts,
        "skills": skills,
        "bosses": bosses,
        "sources": sources,
        "items": table["items"],
        "member_items": table["member_items"],
        "item_names": table.get("item_names", {}),
        "big_drops": big_drops,
        "feeds": feeds,
        "activity": activity,
    }


# --------------------------------------------------------------------------- #
# Cached entry point (async; for the web API and the bot)
# --------------------------------------------------------------------------- #
def _redis():
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception:
        return None


def cache_key(group_id: int, period: str) -> str:
    return f"{CACHE_PREFIX}{int(group_id)}:{period}"


def invalidate(group_id: int) -> None:
    conn = _redis()
    if conn is None:
        return
    try:
        for key in conn.scan_iter(match=f"{CACHE_PREFIX}{int(group_id)}:*", count=200):
            conn.delete(key)
    except Exception:
        pass


async def get_group_stats(group_id: int, period: Optional[str]) -> Optional[dict]:
    """The cached payload; computes (once, under a lock) on a miss.

    Returns None for an unknown group or a period we don't serve.
    """
    import asyncio

    window = parse_period(period)
    if window is None:
        return None
    conn = _redis()
    key = cache_key(group_id, window.period)
    if conn is not None:
        try:
            raw = conn.get(key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass

    lock_key = f"{key}:lock"
    got_lock = True
    if conn is not None:
        try:
            got_lock = bool(conn.set(lock_key, "1", nx=True, ex=120))
        except Exception:
            got_lock = True
    if not got_lock:
        for _ in range(40):
            await asyncio.sleep(0.5)
            try:
                raw = conn.get(key)
                if raw:
                    return json.loads(raw)
            except Exception:
                break

    try:
        from db import Session

        session = Session()
        try:
            from services.recap import _visible_group_player_ids

            player_ids = _visible_group_player_ids(session, group_id)
            wom = await prepare_wom(session, group_id, window, player_ids)
            session.rollback()
            payload = await asyncio.to_thread(_compute_in_thread, group_id, window, wom)
        finally:
            try:
                session.close()
            except Exception:
                pass
        if payload is not None and conn is not None:
            try:
                conn.setex(key, cache_ttl(window),
                           json.dumps(payload, separators=(",", ":"), default=str))
            except Exception:
                pass
        return payload
    finally:
        if conn is not None and got_lock:
            try:
                conn.delete(lock_key)
            except Exception:
                pass


def _compute_in_thread(group_id: int, window: Window, wom: WomInputs) -> Optional[dict]:
    from db import Session

    session = Session()
    try:
        return compute(session, group_id, window, wom)
    finally:
        try:
            session.rollback()
            session.close()
        except Exception:
            pass
