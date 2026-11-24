"""Quest reference data: the name behind each quest id (suggestion #258).

Account sync stores one row per player per quest in ``player_quest_states``,
keyed by the game's quest id, and nothing on the server knew what the ids
meant. ``data/quests.json`` is that map, taken from RuneLite's ``Quest`` enum
(the same list the plugin polls) by ``scripts/sync_quest_names.py``.

Two kinds of id are not in the map and they are not the same thing:

* **Retired** ids are ones the game no longer uses (a quest that was renumbered
  keeps its old rows forever, because a sync only writes the ids it polls).
  They are listed explicitly in the file and are dropped from lists *and*
  counts, or the renumbered quest would be counted twice.
* **Unknown** ids are quests newer than the file. They stay in the list with a
  null name and still count, so a new release understates nothing.

Stdlib-only and free of ``db`` imports: the Data API process loads it too.
"""
from __future__ import annotations

import json
import os
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "quests.json")

STATE_NAMES = {0: "not_started", 1: "in_progress", 2: "finished"}

#: (names, retired ids, list position of each named id)
_reference: Optional[Tuple[Dict[int, str], FrozenSet[int], Dict[int, int]]] = None


def _sort_key(name: str) -> str:
    """The in-game quest list's order: alphabetical, ignoring a leading article."""
    folded = name.lower()
    for article in ("the ", "a ", "an "):
        if folded.startswith(article):
            return folded[len(article):]
    return folded


def _load() -> Tuple[Dict[int, str], FrozenSet[int], Dict[int, int]]:
    global _reference
    if _reference is None:
        names: Dict[int, str] = {}
        retired: FrozenSet[int] = frozenset()
        try:
            with open(_PATH, encoding="utf-8") as fh:
                loaded = json.load(fh)
            for quest in loaded.get("quests", []):
                names[int(quest["id"])] = str(quest["name"])
            retired = frozenset(int(i) for i in loaded.get("retired", []))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            names, retired = {}, frozenset()
        ordered = sorted(names, key=lambda i: (_sort_key(names[i]), i))
        _reference = (names, retired, {quest_id: n for n, quest_id in enumerate(ordered)})
    return _reference


def quest_names() -> Dict[int, str]:
    """``quest_id -> name``; ``{}`` if the file is gone."""
    return _load()[0]


def retired_quest_ids() -> FrozenSet[int]:
    return _load()[1]


def quest_list(states: Mapping[int, int]) -> List[dict]:
    """One player's ``{quest_id: state}`` as a sorted list of
    ``{"id", "name", "state"}``, state as its label.

    Retired ids and states outside 0-2 are dropped. Named quests come first in
    the game's order; ids the file does not know follow, by id, with a null
    name.
    """
    names, retired, position = _load()
    unknown_from = len(position)
    keyed = []
    for quest_id, state in states.items():
        label = STATE_NAMES.get(state)
        if label is None or quest_id in retired:
            continue
        # Unknown ids sort after every named quest, by id.
        keyed.append((position.get(quest_id, unknown_from + quest_id), quest_id, label))
    keyed.sort()
    return [{"id": quest_id, "name": names.get(quest_id), "state": label}
            for _, quest_id, label in keyed]


def count_states(quests: List[dict]) -> Dict[str, int]:
    """Counts by state label for a ``quest_list`` result."""
    counts = {"not_started": 0, "in_progress": 0, "finished": 0}
    for quest in quests:
        counts[quest["state"]] += 1
    return counts
