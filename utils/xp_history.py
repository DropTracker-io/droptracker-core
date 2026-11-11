"""Skill levels and XP gains for player profiles.

Pure arithmetic over ``player_exp`` (latest XP) and baselines read from
``player_stat_snapshots``. The store itself, and its one writer, live in
`utils.stat_snapshots`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

# The order the game's stats panel lays skills out in, row by row.
SKILLS = [
    "attack", "hitpoints", "mining",
    "strength", "agility", "smithing",
    "defence", "herblore", "fishing",
    "ranged", "thieving", "cooking",
    "prayer", "crafting", "firemaking",
    "magic", "fletching", "woodcutting",
    "runecraft", "slayer", "farming",
    "construction", "hunter", "sailing",
]

MAX_LEVEL = 99

# Rolling windows, as Wise Old Man presents them.
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}


def _build_xp_table() -> List[int]:
    """XP needed for each level 1..99 (index = level)."""
    table = [0, 0]
    points = 0
    for level in range(1, MAX_LEVEL):
        points += int(level + 300 * 2 ** (level / 7.0))
        table.append(points // 4)
    return table


_XP_FOR_LEVEL = _build_xp_table()


def level_for_xp(xp: Any) -> int:
    """The real (not virtual) level for an XP total: 1..99."""
    try:
        value = int(xp)
    except (TypeError, ValueError):
        return 1
    level = 1
    # 99 entries; a bisect would save nothing worth the import.
    for candidate in range(2, MAX_LEVEL + 1):
        if value >= _XP_FOR_LEVEL[candidate]:
            level = candidate
        else:
            break
    return level


def xp_for_level(level: int) -> int:
    return _XP_FOR_LEVEL[max(1, min(MAX_LEVEL, int(level)))]


def skills_from_row(row: Any) -> Dict[str, int]:
    """``{skill: xp}`` from a ``player_exp`` row (or anything with the attrs)."""
    out = {}
    for skill in SKILLS:
        try:
            out[skill] = max(0, int(getattr(row, skill, 0) or 0))
        except (TypeError, ValueError):
            out[skill] = 0
    return out


def skill_summary(xp: Mapping[str, int]) -> dict:
    """Levels, totals and progress to the next level for the stats panel."""
    skills = []
    total_level = 0
    total_xp = 0
    for skill in SKILLS:
        value = int(xp.get(skill, 0) or 0)
        level = level_for_xp(value)
        # Hitpoints starts at 10; an unsynced 0 would read as level 1.
        if skill == "hitpoints":
            level = max(level, 10)
        total_level += level
        total_xp += value
        if level >= MAX_LEVEL:
            progress = 100.0
        else:
            floor, ceiling = xp_for_level(level), xp_for_level(level + 1)
            progress = round(max(0.0, min(100.0, (value - floor) / (ceiling - floor) * 100)), 1)
        skills.append({"skill": skill, "xp": value, "level": level, "progress": progress})
    return {"skills": skills, "total_level": total_level, "total_xp": total_xp}


Baseline = Tuple[datetime, Mapping[str, int]]


def period_starts(now: datetime) -> Dict[str, datetime]:
    """When each rolling period began, counted back from ``now``."""
    return {period: now - timedelta(days=days) for period, days in PERIOD_DAYS.items()}


def gains_since(current: Mapping[str, int], baselines: Mapping[str, Optional[Baseline]],
                earliest: Optional[Baseline], now: datetime) -> Dict[str, Optional[dict]]:
    """XP gained over each rolling period, or None where there is no baseline.

    ``baselines[period]`` is the latest snapshot at or before that period's
    start, as ``(taken_at, {skill: xp})``. Snapshots are only written when XP
    changes, so that row is the XP at the start. A player whose history
    begins inside the period gets ``earliest`` (their first snapshot)
    instead, and ``since`` says how far back it really goes.
    """
    starts = period_starts(now)
    out: Dict[str, Optional[dict]] = {}
    for period, start in starts.items():
        baseline = baselines.get(period)
        partial = False
        if baseline is None:
            if earliest is None:
                out[period] = None
                continue
            baseline, partial = earliest, True
        taken_at, base_xp = baseline
        skills = {}
        total = 0
        for skill in SKILLS:
            if skill not in base_xp:
                continue
            gained = int(current.get(skill, 0) or 0) - int(base_xp[skill])
            if gained > 0:
                skills[skill] = gained
                total += gained
        out[period] = {
            "total": total,
            "skills": skills,
            "since": int((taken_at if partial else start).timestamp()),
            "partial": partial,
        }
    return out
