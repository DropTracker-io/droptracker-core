"""Doom of Mokhaiotl delve levels: naming, NPC rows and the deepest-delve record.

The game prints one line per delve level completed::

    Delve level: 3 duration: 1:23. Personal best: 1:05
    Delve level 1 - 8 duration: 12:05. Personal best: 11:56
    Delve level: 8+ (9) duration: 1:27 (new personal best)

Levels 1-8 each have their own personal best. Every level past 8 shares ONE
personal best, "8+", and the level actually completed is only in the
parentheses. The plugin sends the timed lines as PB submissions named
``Doom of Mokhaiotl (Level:N)`` / ``(Level:8+)`` (plus ``delve_level`` for 8+
since 6.0.15); the 1 - 8 total arrives as plain ``Doom of Mokhaiotl``.

NPC rows: 14707 is the boss itself (the 1 - 8 total), 14707 + N is level N
(1-8) and 14716 is the shared 8+ board.

``player_deepest_delve`` keeps the deepest level each player has COMPLETED.
It only ever moves up. ``exact`` is false when all we know is "some level
past 8" (a client older than 6.0.15, or the backfill), shown as "9+".
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import text

DOOM_NPC_ID = 14707
DEEP_NPC_ID = 14716
DEEP_NPC_NAME = "Doom of Mokhaiotl (Level: 8+)"
#: Every npc_list row that belongs to Doom: the boss, levels 1-8 and 8+.
DOOM_NPC_IDS = frozenset(range(DOOM_NPC_ID, DEEP_NPC_ID + 1))
#: The level the 8+ board stands for when the real level is unknown.
DEEP_FLOOR = 9
#: The deepest level we accept from a client; anything past it is a bad parse.
MAX_LEVEL = 200

_LEVEL_RE = re.compile(r"\(\s*Level\s*:?\s*(\d+)\s*(\+?)\s*\)", re.IGNORECASE)


def is_doom_name(npc_name: Optional[str]) -> bool:
    return bool(npc_name) and "doom of mokhaiotl" in npc_name.lower()


def resolve_level_name(npc_name: str) -> Optional[Tuple[int, str]]:
    """``(npc_id, display name)`` for a Doom level name, else None.

    ``Doom of Mokhaiotl (Level:3)`` -> ``(14710, "Doom of Mokhaiotl (Level 3)")``;
    ``Doom of Mokhaiotl (Level:8+)`` -> the shared 8+ row. The plain boss name
    is not a level name and returns None.
    """
    if not is_doom_name(npc_name):
        return None
    match = _LEVEL_RE.search(npc_name)
    if not match:
        return None
    level, plus = int(match.group(1)), bool(match.group(2))
    if plus:
        return DEEP_NPC_ID, "Doom of Mokhaiotl (Level 8+)"
    if not 1 <= level <= 8:
        return None
    return DOOM_NPC_ID + level, f"Doom of Mokhaiotl (Level {level})"


def completed_level(npc_id: Optional[int], reported_level) -> Optional[Tuple[int, bool]]:
    """``(level, exact)`` a Doom PB submission proves was completed, else None.

    ``npc_id`` is the resolved row; ``reported_level`` is the plugin's
    ``delve_level`` field (6.0.15+), which only the 8+ board needs.
    """
    if npc_id is None or npc_id not in DOOM_NPC_IDS or npc_id == DOOM_NPC_ID:
        # The plain boss row is the 1 - 8 total from new clients, but older
        # clients also sent 8+ splits under it, so it proves nothing on its own.
        return None
    if npc_id != DEEP_NPC_ID:
        return npc_id - DOOM_NPC_ID, True
    try:
        level = int(str(reported_level).strip())
    except (TypeError, ValueError):
        level = 0
    if DEEP_FLOOR <= level <= MAX_LEVEL:
        return level, True
    return DEEP_FLOOR, False


def format_level(level: int, exact: bool) -> str:
    return f"{level}" if exact else f"{level}+"


_UPSERT = text(
    """
    INSERT INTO player_deepest_delve (player_id, deepest_level, exact, achieved_at, updated_at)
    VALUES (:player_id, :level, :exact, :at, :at)
    ON DUPLICATE KEY UPDATE
        exact = IF(:level > deepest_level OR (:level = deepest_level AND :exact > exact),
                   :exact, exact),
        achieved_at = IF(:level > deepest_level OR (:level = deepest_level AND :exact > exact),
                         :at, achieved_at),
        deepest_level = GREATEST(deepest_level, :level),
        updated_at = :at
    """
)


def record_completed(session, player_id: int, level: int, exact: bool,
                     at: Optional[datetime] = None) -> None:
    """Raise a player's deepest delve to ``level`` if it is deeper.

    One atomic upsert, so two submissions racing for the same player can
    neither lose the deeper level nor trip over the primary key. MySQL applies
    the assignments left to right, which is why ``deepest_level`` is last: the
    tests above it compare against the stored value. An exact level replaces
    an inexact one of the same depth ("9+" becomes "9").
    """
    session.execute(_UPSERT, {
        "player_id": int(player_id),
        "level": int(level),
        "exact": 1 if exact else 0,
        "at": at or datetime.now(),
    })
