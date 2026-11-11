"""Achievement diary tasks: the reference list and how per-task state is decided.

The plugin syncs a *count* of completed tasks per diary tier (the game's own
``diary_completion_info`` script), not which tasks. A profile wants the
journal the game shows: every task, struck through when done. Three levels of
knowledge get it there, most certain first:

1. A tier whose count equals its size is complete: every task is done.
2. A tier whose count is 0 has nothing done.
3. Anything in between needs to know *which* tasks. The OSRS Wiki's WikiSync
   plugin publishes that per player, as booleans in the wiki's table order
   (the same order ``data/diary_tasks.json`` is scraped in). It is used only
   for a tier where its count of done tasks **equals ours**: WikiSync data is
   as old as that player's last WikiSync upload, and a stale list would mark
   the wrong tasks. Where it disagrees or is absent, tasks stay ``None``
   ("unknown") and the tier still shows its true count.

Everything here is pure; the HTTP fetch lives in ``utils/wikisync.py``.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "diary_tasks.json")

TIER_NAMES = ["Easy", "Medium", "Hard", "Elite"]

_reference: Optional[List[dict]] = None


def diary_reference() -> List[dict]:
    """The twelve areas with their tiers and tasks; ``[]`` if the file is gone."""
    global _reference
    if _reference is None:
        try:
            with open(_PATH, encoding="utf-8") as fh:
                loaded = json.load(fh)
            areas = loaded.get("areas") if isinstance(loaded, dict) else None
            _reference = areas if isinstance(areas, list) else []
        except (OSError, ValueError):
            _reference = []
    return _reference


def tier_totals() -> Dict[Tuple[int, int], int]:
    """``(area_id, tier) -> number of tasks``."""
    return {
        (area["area_id"], tier["tier"]): len(tier["tasks"])
        for area in diary_reference()
        for tier in area["tiers"]
    }


def _wikisync_tasks(wikisync: Optional[Mapping[str, Any]], area_key: str,
                    tier_name: str, size: int) -> Optional[List[bool]]:
    """WikiSync's booleans for one tier, or None unless they are usable."""
    if not isinstance(wikisync, Mapping):
        return None
    area = wikisync.get(area_key)
    tier = area.get(tier_name) if isinstance(area, Mapping) else None
    tasks = tier.get("tasks") if isinstance(tier, Mapping) else None
    if not isinstance(tasks, list) or len(tasks) != size:
        return None
    if not all(isinstance(t, bool) for t in tasks):
        return None
    return tasks


def merge_diaries(counts: Mapping[Tuple[int, int], int],
                  wikisync: Optional[Mapping[str, Any]] = None,
                  reference: Optional[List[dict]] = None) -> List[dict]:
    """Every area and tier with its tasks and what is known about each.

    ``counts`` is ``(area_id, tier) -> completed`` from ``player_diary_tiers``;
    a missing key means that tier was never synced (``completed`` None).
    ``wikisync`` is the ``achievement_diaries`` object of a WikiSync profile.

    Each task carries ``completed``: True, False, or None when only the tier's
    count is known. ``tasks_known`` on the tier says which of those it is.
    """
    out = []
    for area in reference if reference is not None else diary_reference():
        tiers = []
        for tier in area["tiers"]:
            size = len(tier["tasks"])
            raw = counts.get((area["area_id"], tier["tier"]))
            completed = None if raw is None else max(0, min(int(raw), size))

            states: List[Optional[bool]]
            source = None
            if completed is None:
                states = [None] * size
            elif completed >= size:
                states, source = [True] * size, "sync"
            elif completed == 0:
                states, source = [False] * size, "sync"
            else:
                ws = _wikisync_tasks(wikisync, area.get("wikisync") or area["name"],
                                     tier["name"], size)
                if ws is not None and sum(ws) == completed:
                    states, source = list(ws), "wikisync"
                else:
                    states = [None] * size

            tiers.append({
                "tier": tier["tier"],
                "name": tier["name"],
                "completed": completed,
                "total": size,
                "complete": completed is not None and completed >= size,
                "tasks_known": source is not None,
                "task_source": source,
                "tasks": [
                    {
                        "description": task.get("description") or "",
                        "requirements": task.get("requirements") or [],
                        "note": task.get("note"),
                        "completed": state,
                    }
                    for task, state in zip(tier["tasks"], states)
                ],
            })
        done = sum(t["completed"] or 0 for t in tiers)
        out.append({
            "area_id": area["area_id"],
            "name": area["name"],
            "completed": done,
            "total": sum(t["total"] for t in tiers),
            "tiers": tiers,
        })
    return out
