"""Combat achievement speed targets, for "is this PB a Grandmaster time?".

Read from ``scripts/ca_tasks.json`` (the CA catalogue extracted from the game
cache; see ``scripts/cache/extract-ca-tasks.mjs``), so a rebuild after a game
update picks up new speed tasks without a code change. Each Speed task's
description is parsed into ``(boss, team size, target time)``:

    "Kill Vorkath in less than 54 seconds."                 -> Vorkath, any, < 0:54
    "Complete the Theatre of Blood (Trio) in less than ..." -> ToB, "3", < ...
    "Complete the Colosseum with a total time of 24:00 or less." -> <= 24:00

A task that is not one kill against the clock cannot be checked against a
personal best and is left out: kill counts inside a window ("Kill Araxxor 6
times in 10:00"), averages (Yama), "the target time", player-count limits our
boards cannot see (Hueycoatl), and unreadable wording. ``SKIPPED`` keeps the
reason for each, and the unit tests fail when a rebuilt catalogue adds a task
that neither parses nor is skipped on purpose.

Boards are matched with ``utils.npc_names.npc_match_key``, so "Crystalline
Hunllef" finds The Gauntlet board.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Dict, List, NamedTuple, Optional, Tuple

TASKS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "ca_tasks.json"
)

#: Tiers a speed filter can ask for, easiest first.
TIERS = ("elite", "master", "grandmaster")
TIER_LABELS = {"elite": "Elite", "master": "Master", "grandmaster": "Grandmaster"}
_TIER_RANK = {tier: i for i, tier in enumerate(TIERS)}

#: CA monster names that are not the name of our board.
MONSTER_BOARDS = {
    "Chambers of Xeric: CM": "Chambers of Xeric Challenge Mode",
}

_TEAM_WORDS = {"solo": "Solo", "duo": "2", "trio": "3"}
_TEAM_PAREN = re.compile(r"\((Solo|Duo|Trio|(\d+)-scale)\)", re.IGNORECASE)
_GROUP_OF = re.compile(r"in a group of (\d+)", re.IGNORECASE)
#: Team wording that would otherwise read as the time ("... of 8").
_GROUP_PHRASE = re.compile(r"\s*(?:in a group of \d+|at any group size)", re.IGNORECASE)
_DOOM_LEVEL = re.compile(r"Doom of Mokhaiotl level (\d+)\b", re.IGNORECASE)

#: Wording that means the task is not one timed kill.
_NOT_ONE_KILL = (
    (re.compile(r"\b\d+ times\b", re.IGNORECASE), "several kills in a window"),
    (re.compile(r"\baverage\b", re.IGNORECASE), "an average over several kills"),
    (re.compile(r"first \d+ kills", re.IGNORECASE), "several kills in a window"),
    (re.compile(r"(target|challenge) time", re.IGNORECASE), "a time the description does not give"),
    (re.compile(r"\bplayers\b", re.IGNORECASE), "a player count our boards cannot see"),
    (re.compile(r"TzHaar-Ket-Rak", re.IGNORECASE), "no personal best board"),
)

_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_MINUTES = re.compile(r"(\d+)\s*(?:minutes?|mins?)\b", re.IGNORECASE)
_SECONDS = re.compile(r"(\d+)\s*seconds?\b", re.IGNORECASE)
_LIMIT = re.compile(r"\b(?:less than|under|within|in|of)\s+", re.IGNORECASE)


class Target(NamedTuple):
    tier: str
    time_ms: int
    #: "24:00 or less" / "within 6 minutes" include the target itself.
    inclusive: bool
    task: str


def parse_duration(text: str) -> Optional[int]:
    """Milliseconds from task wording: "1:40 minutes", "1 minute and 30 seconds",
    "54 seconds", "65 minutes", "18 mins", "7:15"."""
    clock = _CLOCK.search(text)
    if clock:
        return (int(clock.group(1)) * 60 + int(clock.group(2))) * 1000
    minutes, seconds = _MINUTES.search(text), _SECONDS.search(text)
    if not minutes and not seconds:
        return None
    total = (int(minutes.group(1)) * 60 if minutes else 0) + (int(seconds.group(1)) if seconds else 0)
    return total * 1000 or None


def parse_task(task: dict) -> Tuple[Optional[Tuple[str, Optional[str], Target]], Optional[str]]:
    """``((board name, team size or None for any, Target), None)`` or ``(None, reason)``."""
    tier = str(task.get("tier") or "").strip().lower()
    if tier not in _TIER_RANK:
        return None, f"tier {task.get('tier')!r} is below Elite"
    description = " ".join(str(task.get("description") or "").split())
    for pattern, reason in _NOT_ONE_KILL:
        if pattern.search(description):
            return None, reason
    # The time follows the LAST "less than" / "in" / "of": "Chambers of
    # Xeric (Solo) in less than 21 minutes".
    head = _GROUP_PHRASE.sub("", description.split(" without ")[0])
    limits = list(_LIMIT.finditer(head))
    time_ms = parse_duration(head[limits[-1].end():]) if limits else None
    if not time_ms:
        return None, "no time in the description"

    monster = str(task.get("monster") or "").strip()
    doom = _DOOM_LEVEL.search(description)
    if doom:
        monster = f"Doom of Mokhaiotl (Level {doom.group(1)})"
    board = MONSTER_BOARDS.get(monster, monster)
    if not board:
        return None, "no monster"

    team = None
    paren = _TEAM_PAREN.search(description)
    group_of = _GROUP_OF.search(description)
    if paren:
        team = paren.group(2) or _TEAM_WORDS[paren.group(1).lower()]
    elif group_of:
        team = "Solo" if group_of.group(1) == "1" else group_of.group(1)

    inclusive = bool(re.search(r"\bor less\b|\bwithin\b", description, re.IGNORECASE))
    return (board, team, Target(tier, time_ms, inclusive, str(task.get("name") or ""))), None


def _tasks() -> List[dict]:
    try:
        with open(TASKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else data
    return [t for t in tasks or [] if isinstance(t, dict) and t.get("type") == "Speed"]


@lru_cache(maxsize=1)
def targets() -> Dict[Tuple[str, Optional[str]], List[Target]]:
    """``{(board match key, team size or None): [Target, ...]}``."""
    from utils.npc_names import npc_match_key

    out: Dict[Tuple[str, Optional[str]], List[Target]] = {}
    for task in _tasks():
        parsed, _reason = parse_task(task)
        if parsed:
            board, team, target = parsed
            out.setdefault((npc_match_key(board), team), []).append(target)
    return out


def _board_targets(npc_name, team_size) -> List[Target]:
    from utils.npc_names import npc_match_key
    from utils.pb_rank import normalize_team_size

    if not npc_name:
        return []
    key = npc_match_key(str(npc_name))
    team = normalize_team_size(team_size)
    table = targets()
    return table.get((key, None), []) + (table.get((key, team), []) if team else [])


def _beats(time_ms: int, target: Target) -> bool:
    return time_ms <= target.time_ms if target.inclusive else time_ms < target.time_ms


def speed_tier(npc_name, team_size, time_ms) -> Optional[str]:
    """The hardest CA speed tier this time meets on its board, or None."""
    if not time_ms or int(time_ms) <= 0:
        return None
    met = [t.tier for t in _board_targets(npc_name, team_size) if _beats(int(time_ms), t)]
    return max(met, key=_TIER_RANK.__getitem__) if met else None


def meets_tier(npc_name, team_size, time_ms, tier: str) -> bool:
    """Whether the time meets ``tier`` or a harder one. A boss with no speed
    task at that level (or above) never does."""
    met = speed_tier(npc_name, team_size, time_ms)
    return met is not None and tier in _TIER_RANK and _TIER_RANK[met] >= _TIER_RANK[tier]
