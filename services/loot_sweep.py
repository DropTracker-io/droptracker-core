"""Loot Sweep scoring (the ``loot_sweep`` event kind).

A Loot Sweep is a race to obtain items from across the game. Unlike a standard
task that *completes* once and awards a flat sum, a loot_sweep task accrues
points continuously and never "completes":

- Each configured **item** awards ``points`` the first time a team receives it,
  then **decays** on every successive receipt (default: −20 percentage points
  of the base each time → 100 / 80 / 60 / 40 / 20 % — the authoring grid the
  admin screenshot shows), down to a per-item **cap** (``max_awards``) after
  which further copies score nothing.
- Items belong to a boss/"**set**". When a team has obtained every set item at
  least once it earns a flat **set bonus** (``set_bonus_points``), repeatable up
  to ``set_bonus_max`` times (a second full set → the bonus again).

One :class:`~db.models.events.EventTask` (``type='loot_sweep'``) models one set;
its ``config`` JSON (``kind='loot_sweep'``) carries the item list and the set
parameters. Standalone items with no set bonus are just a task whose
``set_bonus_points`` is 0.

Scoring is a **pure function of the applied ledger** — like the ``all_of`` /
``groups`` rollups in :mod:`services.event_engine`, the team total is recomputed
from every surviving ``EventCompletion`` row for a (task, team), so apply and
revoke are simple delta adjustments and can never drift. Nothing here does I/O,
so it stays unit-testable (the pytest conftest stubs ``db``/``services``).

The engine wiring (matcher / apply / revoke) lives in
:mod:`services.event_engine`; the write-time config validation lives in
:mod:`web_api.routes.event_task_validation`. See ``docs/LOOT_SWEEP.md``.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

# ---- config defaults (mirrored by the web validator) ----------------------
DEFAULT_DECAY_PERCENT = 20      # percentage points shed per successive receipt
DEFAULT_DECAY_MODE = "linear"   # "linear" | "geometric"
DEFAULT_MAX_AWARDS = 5          # per-item receipts that still score
DEFAULT_SET_BONUS_MAX = 1       # times a full set pays out per team

DECAY_MODES = ("linear", "geometric")

# Bounds the write validator enforces (kept here so scoring and validation
# agree on what a legal config looks like).
MAX_ITEMS = 100
MIN_ITEM_POINTS = 1
MAX_ITEM_POINTS = 1_000_000
MAX_AWARDS_CAP = 100
MAX_SET_BONUS_POINTS = 10_000_000
MAX_SET_BONUS_MAX = 100


def _norm(value) -> str:
    """Case-insensitive name key — identical to ``event_engine._norm`` so a
    ledger row's ``matched_target`` lines up with a config item name."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #
class LootSweepConfig:
    """Normalized view of a loot_sweep task config (defaults resolved).

    ``items`` is a list of dicts with normalized keys: ``key`` (norm name),
    ``name`` (display), ``item_id`` (int|None), ``points`` (float base),
    ``max_awards`` (int), ``counts_for_set`` (bool)."""

    __slots__ = (
        "decay_percent", "decay_mode", "default_max_awards",
        "set_bonus_points", "set_bonus_max", "items", "by_key",
    )

    def __init__(self, config):
        # Accept a parsed dict, a JSON string (EventTask.config as stored), or
        # None — so callers can hand the raw column straight in.
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        config = config if isinstance(config, dict) else {}
        self.decay_percent = _clamp_percent(config.get("decay_percent"))
        mode = config.get("decay_mode")
        self.decay_mode = mode if mode in DECAY_MODES else DEFAULT_DECAY_MODE
        self.default_max_awards = _clamp_max_awards(
            config.get("default_max_awards"), DEFAULT_MAX_AWARDS)
        self.set_bonus_points = max(_int(config.get("set_bonus_points"), 0), 0)
        self.set_bonus_max = _clamp_max_awards(
            config.get("set_bonus_max"), DEFAULT_SET_BONUS_MAX)

        self.items: list[dict] = []
        self.by_key: dict[str, dict] = {}
        for raw in (config.get("items") or []):
            item = self._parse_item(raw)
            if item is None or item["key"] in self.by_key:
                continue
            self.items.append(item)
            self.by_key[item["key"]] = item

    def _parse_item(self, raw) -> Optional[dict]:
        if isinstance(raw, str):
            name, entry = raw, {}
        elif isinstance(raw, dict):
            name = raw.get("item_name") or raw.get("name") or ""
            entry = raw
        else:
            return None
        key = _norm(name)
        if not key:
            return None
        points = _num(entry.get("points"), 1.0)
        max_awards = entry.get("max_awards")
        return {
            "key": key,
            "name": str(name).strip(),
            "item_id": _int(entry.get("item_id"), 0) or None,
            "points": points if points > 0 else 1.0,
            "max_awards": (_clamp_max_awards(max_awards, self.default_max_awards)
                           if max_awards is not None else self.default_max_awards),
            # Set membership: default True. Standalone extras (e.g. the pet)
            # set this False so they never gate set completion.
            "counts_for_set": entry.get("counts_for_set", True) is not False,
        }

    @property
    def set_item_keys(self) -> list[str]:
        return [it["key"] for it in self.items if it["counts_for_set"]]

    @property
    def set_enabled(self) -> bool:
        return self.set_bonus_points > 0 and bool(self.set_item_keys)


def _clamp_percent(value) -> int:
    pct = _int(value, DEFAULT_DECAY_PERCENT)
    if value is None:
        pct = DEFAULT_DECAY_PERCENT
    return min(max(pct, 0), 100)


def _clamp_max_awards(value, default: int) -> int:
    n = _int(value, default)
    if n < 1:
        n = default
    return min(n, MAX_AWARDS_CAP)


# --------------------------------------------------------------------------- #
# Award math
# --------------------------------------------------------------------------- #
def receipt_factor(k: int, decay_percent: int, decay_mode: str = DEFAULT_DECAY_MODE) -> float:
    """Multiplier applied to an item's base points on its ``k``-th receipt
    (1-indexed). ``linear`` sheds ``decay_percent`` percentage-points of the
    base each time (100/80/60/40/20 for 20); ``geometric`` multiplies by
    ``(1 − decay_percent/100)`` each time (100/80/64/51.2 for 20). Never
    negative."""
    if k < 1:
        return 0.0
    if decay_mode == "geometric":
        return max(0.0, (1.0 - decay_percent / 100.0) ** (k - 1))
    return max(0.0, 1.0 - (k - 1) * decay_percent / 100.0)


def item_points(base: float, count: int, max_awards: int,
                decay_percent: int, decay_mode: str = DEFAULT_DECAY_MODE) -> int:
    """Total points an item is worth to a team that has received it ``count``
    times: sum of the first ``min(count, max_awards)`` decayed receipts, each
    receipt rounded to a whole point (so the grid columns are clean integers)."""
    scored = min(max(count, 0), max(max_awards, 0))
    total = 0
    for k in range(1, scored + 1):
        total += int(round(base * receipt_factor(k, decay_percent, decay_mode)))
    return total


# --------------------------------------------------------------------------- #
# Team scoring (from ledger receipt counts)
# --------------------------------------------------------------------------- #
def counts_from_rows(rows: Iterable) -> dict[str, int]:
    """``{normalized item name: total receipts}`` from applied ledger rows.

    Each unit of ``quantity`` is one receipt (a stack of 3 counts as 3). Bonus
    rows and rows without a ``matched_target`` are ignored — loot_sweep only
    scores identified item receipts."""
    counts: dict[str, int] = {}
    for r in rows:
        if (getattr(r, "source_type", None) or "") == "bonus":
            continue
        key = _norm(getattr(r, "matched_target", None))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + max(_int(getattr(r, "quantity", 1), 1), 1)
    return counts


def score_counts(counts: dict[str, int], config: LootSweepConfig) -> dict:
    """Full breakdown for a team's receipt counts. Returns::

        {"total": int, "item_total": int, "set_total": int,
         "sets_completed": int, "sets_awarded": int,
         "items": [{item_id, name, key, count, scored, points, max_awards,
                    counts_for_set}]}

    ``total`` is what the team's :class:`EventTeam` score should reflect for
    this task; ``items`` powers the per-item / per-team authoring grid and the
    live board. Items appear in config order; unknown ledger names are ignored
    (they can't score)."""
    item_rows = []
    item_total = 0
    for it in config.items:
        count = max(_int(counts.get(it["key"]), 0), 0)
        pts = item_points(it["points"], count, it["max_awards"],
                          config.decay_percent, config.decay_mode)
        item_total += pts
        item_rows.append({
            "item_id": it["item_id"],
            "name": it["name"],
            "key": it["key"],
            "count": count,
            "scored": min(count, it["max_awards"]),
            "points": pts,
            "max_awards": it["max_awards"],
            "counts_for_set": it["counts_for_set"],
        })

    sets_completed = 0
    sets_awarded = 0
    set_total = 0
    if config.set_enabled:
        sets_completed = min(max(_int(counts.get(k), 0), 0) for k in config.set_item_keys)
        sets_awarded = min(sets_completed, config.set_bonus_max)
        set_total = sets_awarded * config.set_bonus_points

    return {
        "total": item_total + set_total,
        "item_total": item_total,
        "set_total": set_total,
        "sets_completed": sets_completed,
        "sets_awarded": sets_awarded,
        "items": item_rows,
    }


def score_rows(rows: Iterable, config: LootSweepConfig) -> dict:
    """Convenience: :func:`score_counts` straight from ledger rows."""
    return score_counts(counts_from_rows(rows), config)


def team_total(rows: Iterable, config: LootSweepConfig) -> int:
    """Just the integer team-score contribution for a (task, team)."""
    return score_rows(rows, config)["total"]
