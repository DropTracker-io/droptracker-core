# utils/per_drop.py — items that count once per drop, whatever the stack size.
#
# Some drops arrive in stacks the organizer wants counted as ONE: Hueycoatl
# hides (usually 3 at a time), Colosseum echo crystals. By default every unit
# of a drop's quantity counts (3 hides = 3 toward the goal, or 3 Loot Sweep
# receipts). An organizer flags the item to count each drop once instead.
#
# - item_collection: ``config.per_drop_items`` is a list of the flagged item
#   names (the same shape as ``pet_items``). It covers the single target,
#   flat lists, requirement groups and either-or paths. On a point_collection
#   list a flagged item pays its points once per drop, not points × quantity.
# - loot_sweep: a per-entry ``per_drop: true`` flag (services/loot_sweep.py).
#
# Absent means "quantity counts", so no running task changes on deploy. Only a
# manual award is left alone: there the organizer types the quantity.
#
# Stdlib only, like utils.vestige_rings: the engine and the write validator
# both read this, and the unit-test conftest stubs the whole ``services``
# package, so the shared definitions can't live there.

from __future__ import annotations

from utils.task_progress import norm

# The item_collection task-config key.
CONFIG_KEY = "per_drop_items"


def names(config) -> frozenset:
    """Normalized names an item_collection config counts once per drop.
    Tolerates garbage (empty set)."""
    raw = (config or {}).get(CONFIG_KEY) if isinstance(config, dict) else None
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(norm(n) for n in raw if isinstance(n, str) and norm(n))
