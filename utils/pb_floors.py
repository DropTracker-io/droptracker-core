"""Times no real kill can produce, checked before a personal best is stored.

Some activities print several timed lines per run: the Colosseum prints a
duration for every wave as well as the whole run, and the Doom of Mokhaiotl
prints one per delve level as well as the "Delve level 1 - 8" total. A client
that mis-reads one of the partial lines files it as the whole run, where it
then sits at the top of the board forever, since nothing real can beat it
(bug #140: Colosseum "PBs" of 22s, and 609 single-level times on the Doom 1-8
board).

A floor here is deliberately loose: well under any recorded legitimate time,
far above the partial times it exists to stop. Floors are keyed by
``utils.npc_names.npc_match_key`` so every spelling and alias of the boss is
covered.

Kept dependency-free apart from ``utils.npc_names`` so it is importable from
intake, scripts and tests alike.
"""
from __future__ import annotations

from typing import Iterable, Optional

from utils.npc_names import npc_match_key

#: match key -> fewest milliseconds a whole run can take.
FLOORS_MS = {
    # Twelve waves. The fastest stored run is 14:22; a wave or the Sol fight
    # alone is one to three minutes.
    "fortis-colosseum": 10 * 60_000,
    # The 1 - 8 total. The fastest time ever stored on each level sums to
    # 4:29; a single level is under three minutes.
    "doom-of-mokhaiotl": 4 * 60_000,
    # Raids print a duration for every room as well as the whole raid, and a
    # client that files a room time as the raid sits on top of the board
    # (ticket 521: a 0:42.6 solo Theatre of Blood). Stored runs on every team
    # size start near twelve minutes; room times are under seven.
    "theatre-of-blood": 10 * 60_000 + 30_000,
    "theatre-of-blood-hard-mode": 10 * 60_000 + 30_000,
    "theatre-of-blood-entry-mode": 8 * 60_000,
    # The fastest stored expert raid is 13 minutes, normal 10, entry 8.
    "tombs-of-amascut-expert-mode": 10 * 60_000,
    "tombs-of-amascut": 7 * 60_000,
    "tombs-of-amascut-entry-mode": 6 * 60_000,
}

#: Doom of Mokhaiotl levels 1-8 have their own boards (utils.doom_delve).
DOOM_LEVELS = 8
#: Each displayed level time can be rounded by up to one game tick.
TICK_MS = 600


def floor_ms(npc_name: Optional[str]) -> Optional[int]:
    """The floor for this boss, or None when it has none."""
    return FLOORS_MS.get(npc_match_key(npc_name)) if npc_name else None


def below_floor(npc_name: Optional[str], time_ms: int) -> bool:
    """Whether ``time_ms`` is faster than any whole run of this boss can be."""
    floor = floor_ms(npc_name)
    return floor is not None and 0 < time_ms < floor


def below_level_sum(total_ms: int, level_bests_ms: Iterable[int]) -> bool:
    """Whether a Doom 1 - 8 total beats the player's own level bests added up.

    A run's total is the sum of its eight level times, and each of those is at
    least that level's personal best, so a total below the sum of the bests is
    impossible. Only decidable when the player holds a best on all eight
    levels; otherwise it never rejects.
    """
    bests = [b for b in level_bests_ms if b and b > 0]
    if len(bests) < DOOM_LEVELS:
        return False
    return 0 < total_ms < sum(bests) - DOOM_LEVELS * TICK_MS
