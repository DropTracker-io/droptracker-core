# utils/duplicate_pets.py — whether a duplicate pet counts toward an event task.
#
# The pet processor queues EVERY pet submission for the event engine,
# duplicates included, flagged ``is_new_pet`` (data/submissions/pet.py). A
# duplicate is a pet DropTracker has already recorded for that player: the game
# rolled it again and printed the "funny feeling" message instead of handing
# it over. Whether that roll should count is the organizer's call, per task,
# through ``config.duplicate_pets``.
#
# Absent means each task type keeps the behaviour it had before the switch
# existed, so no running event changes score on deploy:
#   - item_collection: duplicates COUNT. A pet listed in ``pet_items`` has no
#     other credit path, and a "5 of these 4 items" tile needs the duplicate.
#   - pet_collection / loot_sweep: duplicates DON'T count. Those counted
#     acquisitions: "obtain 3 boss pets" wasn't meant to be met by re-killing
#     one boss for a pet the player already owns.
# Only a value that differs from the task type's default is ever stored.
#
# A competition (SOTW/BOTW) pet bonus rule carries the same key on the rule
# itself (off unless ``true``); a bonus rule that embeds a full task reads the
# embedded task's config like any other task.
#
# Stdlib only, like utils.vestige_rings: the engine, the write validator and
# the requirements view all read this, and the unit-test conftest stubs the
# whole ``services`` package, so the shared definitions can't live there.

from __future__ import annotations

import json

# The task-config (and competition pet-rule) key.
CONFIG_KEY = "duplicate_pets"

# Task types that can have a pet as a goal and so honour the key.
PET_TASK_TYPES = ("item_collection", "pet_collection", "loot_sweep")

# Task types where an absent key means duplicates count.
_COUNT_BY_DEFAULT = frozenset({"item_collection"})


def default_for(ttype) -> bool:
    """Whether duplicate pets count on a task of this type when its config
    doesn't say."""
    return ttype in _COUNT_BY_DEFAULT


def _parsed(config):
    """The config as a dict. Accepts the stored JSON string (the task routes
    compare raw before/after configs); anything unreadable is ``{}``."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return {}
    return config if isinstance(config, dict) else {}


def duplicates_count(ttype, config) -> bool:
    """Whether a duplicate pet counts toward a task of type ``ttype`` with
    this config. Only a real boolean overrides the type's default: a stray
    string never changes scoring."""
    value = _parsed(config).get(CONFIG_KEY)
    if isinstance(value, bool):
        return value
    return default_for(ttype)


def has_pet_goal(ttype, config) -> bool:
    """Whether a task can be credited by a pet at all, which is when the
    switch means something: every pet_collection task, an item list with
    pets flagged in ``pet_items``, and a loot sweep with a pet entry."""
    if ttype == "pet_collection":
        return True
    cfg = _parsed(config)
    if ttype == "item_collection":
        pets = cfg.get("pet_items")
        return isinstance(pets, (list, tuple)) and any(
            isinstance(p, str) and p.strip() for p in pets)
    if ttype == "loot_sweep":
        # v2 nests items in groups; a v1 config keeps one flat list.
        groups = [g for g in (cfg.get("groups") or ()) if isinstance(g, dict)]
        groups.append({"items": cfg.get("items") or ()})
        return any(
            isinstance(item, dict) and item.get("source") == "pet"
            for group in groups for item in (group.get("items") or ()))
    return False
