# utils/vestige_rings.py — when a DT2 Gold ring counts as its vestige.
#
# Each ancient vestige (Ultor, Magus, Venator, Bellator) takes three successful
# invisible rolls at its boss, and the first two drop 1× and then 2× Gold ring
# instead of the vestige. By default an event task that lists a vestige credits
# a ring dropped by that vestige's boss AS the vestige (hitting the roll is the
# achievement), and one player's ring/ring/vestige chain credits it once — see
# services/event_engine._ring_vestige_for_task and _dedupe_vestige_chain.
#
# A task opts out with ``config.vestige_rings = false``: a clan that scores Gold
# rings in a task of their own doesn't want the same ring to finish the vestige
# task as well. Absent means ON, so every task built before the setting existed
# keeps the behaviour it was built with, and only ``false`` is ever stored.
#
# Stdlib only, like utils.task_progress: the engine, the write validator and
# the requirements view all read this, and the unit-test conftest stubs the
# whole ``services`` package, so the shared definitions can't live there.

from __future__ import annotations

import json

from utils.task_progress import norm

# The task-config key. Only an explicit ``False`` switches rings off.
CONFIG_KEY = "vestige_rings"

RING_NAME = "gold ring"

# Canonical vestige display name -> normalized names of the one boss that
# drops it (awakened variants included). Verified against recorded drops.
VESTIGE_BOSSES = {
    "Ultor vestige": frozenset({"vardorvis", "vardorvis (awakened)"}),
    "Magus vestige": frozenset({"duke sucellus", "duke sucellus (awakened)"}),
    "Venator vestige": frozenset({"the leviathan", "leviathan (awakened)"}),
    "Bellator vestige": frozenset({"the whisperer", "whisperer (awakened)",
                                   "the whisperer (awakened)"}),
}

VESTIGE_NORMS = frozenset(norm(name) for name in VESTIGE_BOSSES)


def is_vestige(name) -> bool:
    """Whether ``name`` is one of the four ancient vestiges."""
    return norm(name) in VESTIGE_NORMS


def rings_count(config) -> bool:
    """Whether Gold rings count as vestiges under this task config.

    Accepts the parsed dict or the stored JSON string (the task routes compare
    raw before/after configs). Anything that isn't an explicit ``false`` —
    absent key, unparseable JSON, no config at all — reads as ON."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return True
    return not (isinstance(config, dict) and config.get(CONFIG_KEY) is False)
