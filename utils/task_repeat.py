# utils/task_repeat.py — repeatable event tasks.
#
# A task normally completes once per team: the first time its progress reaches
# the threshold it pays its points and every later submission is dropped. A
# REPEATABLE task ("acquire 100k gp from Zulrah", as often as you can) keeps
# counting instead: progress stays a running total and every whole multiple of
# the threshold is one more completion, paying the task's points again.
#
# Config (on web_event_tasks.config):
#   ``repeatable``       true — only a real boolean true is stored.
#   ``max_completions``  optional int >= 2; absent = unlimited.
#
# The completion count is never stored. It is derived from the rollup's
# running total (``floor(progress / threshold)``, capped), so apply, revoke,
# live re-folds and every read surface agree by construction.
# ``EventProgress.completed`` keeps its meaning of "this task can't earn any
# more": a repeatable task only sets it on reaching ``max_completions``, so the
# record gate, the effort freeze and the plugin HUD all keep working unchanged.
#
# Only standard events repeat. Bingo tiles, board-game turns and Conquest tiles
# are built on a task finishing once, and Loot Sweep / SOTW / BOTW already
# score every receipt. A repeat flag on a task in one of those events (copied
# from the library, say) is ignored rather than refused.
#
# Pure: stdlib only, so the web_api validator, the engine and the conftest-
# stubbed test suite all load the real thing (see utils/task_progress.py).

from __future__ import annotations

import json
from typing import Optional

CONFIG_KEY = "repeatable"
MAX_KEY = "max_completions"

REPEATABLE_EVENT_KINDS = ("standard",)

MIN_MAX_COMPLETIONS = 2
MAX_MAX_COMPLETIONS = 10_000

# Task types whose progress is a running count (quantity, kills, XP, GP, pets,
# tasks, qualifying kills). skill_target is a state (a level reached, matched
# again on every later XP drop) and loot_sweep / competition score
# continuously, so none of those can repeat.
REPEATABLE_TASK_TYPES = (
    "item_collection", "kc_target", "xp_target", "loot_value", "pet_collection",
    "ca_target", "slayer_target", "pb_target", "custom", "ehp_target", "ehb_target",
)

# item_collection list kinds that fold quantities. Distinct kinds (all_of,
# assembly, any_of_distinct), groups and either-or paths saturate once every
# item is in, so a second completion could never be earned.
REPEATABLE_LIST_KINDS = (None, "any_of", "point_collection")


def _config(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def eligible(task_type, config) -> bool:
    """Whether a task of this type + config CAN repeat (ignores the flag)."""
    if task_type not in REPEATABLE_TASK_TYPES:
        return False
    config = _config(config)
    if config.get("kind") not in REPEATABLE_LIST_KINDS:
        return False
    if task_type == "pb_target":
        # "times" counts qualifying kills; unique_players / whole_team count
        # distinct people and stop moving once everyone is in.
        return (config.get("mode") or "times") == "times"
    return True


def event_allows(event_kind) -> bool:
    return (event_kind or "standard") in REPEATABLE_EVENT_KINDS


def repeat_cap(task_type, config, event_kind=None) -> Optional[int]:
    """How many times one team may complete the task: ``1`` for an ordinary
    task, ``N`` for "up to N times", ``None`` for unlimited.

    ``event_kind=None`` skips the event-kind check (callers that only hold the
    task); pass the event's kind wherever it is known."""
    config = _config(config)
    if config.get(CONFIG_KEY) is not True:
        return 1
    if event_kind is not None and not event_allows(event_kind):
        return 1
    if not eligible(task_type, config):
        return 1
    cap = config.get(MAX_KEY)
    if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1:
        return cap
    return None


def is_repeatable(cap) -> bool:
    return cap != 1


def completion_count(progress, threshold, completed, cap) -> int:
    """Completions a (task, team) rollup represents.

    Ordinary tasks: 1 once ``completed``, else 0. Repeatable tasks: whole
    multiples of the threshold in the running total, capped."""
    if cap == 1:
        return 1 if completed else 0
    try:
        threshold = max(int(threshold or 1), 1)
        n = int(float(progress or 0) // threshold)
    except (TypeError, ValueError):
        return 0
    n = max(n, 0)
    return min(n, cap) if cap is not None else n


def is_maxed(count, cap) -> bool:
    """Whether the rollup can earn nothing more (its ``completed`` flag)."""
    if cap == 1:
        return count >= 1
    return cap is not None and count >= cap


def cycle_progress(progress, threshold, count, cap) -> float:
    """Progress toward the NEXT completion (display + milestone pings)."""
    if cap == 1 or is_maxed(count, cap):
        return float(progress or 0)
    threshold = max(int(threshold or 1), 1)
    return max(float(progress or 0) - count * threshold, 0.0)


def repeat_suffix(count) -> str:
    """`` (×3)`` on the second and later completions; empty on the first."""
    return f" (×{int(count)})" if count and int(count) >= 2 else ""


def lap_threshold(task_type, target_value, config) -> int:
    """One lap's size for a REPEATABLE task — the engine's
    ``completion_threshold`` restricted to the eligible types (a repeatable
    pb_target is always ``times`` mode, so its lap is the kill count)."""
    if task_type == "pb_target":
        try:
            return max(int(_config(config).get("need") or 1), 1)
        except (TypeError, ValueError):
            return 1
    try:
        return max(int(target_value or 0), 1)
    except (TypeError, ValueError):
        return 1


def rollup_completions(task_type, target_value, config, event_kind,
                       progress, completed) -> int:
    """Completions one stored (task, team) rollup represents — for readers
    that hold the task row and the progress row but not the engine (points
    take-back on delete, standings, API payloads)."""
    cap = repeat_cap(task_type, config, event_kind=event_kind)
    return completion_count(progress, lap_threshold(task_type, target_value, config),
                            completed, cap)
