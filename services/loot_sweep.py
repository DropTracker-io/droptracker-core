"""Loot Sweep scoring (the ``loot_sweep`` event kind) — v2 (nested groups).

A Loot Sweep is a race to obtain items from across the game, scored continuously
(a loot_sweep task never "completes"). v2 adds three things the "All Content"
sheet needs:

- **NPC scoping.** Every item only scores when it drops from its target NPC — a
  Dragon 2h counts at Vet'ion, not from anywhere else. NPCs are declared per
  *group* (below); the engine matcher enforces them, so only valid receipts ever
  reach the ledger and the scoring here stays NPC-agnostic.
- **Nested groups (sub-sets).** A task holds one or more **groups**. A group ties
  a set of items to its source NPC(s) and awards ``bonus_points`` when a team has
  collected every one of its set-items once (repeatable up to ``bonus_max``). A
  simple boss is one group; a meta-set like Barrows is many. Completing **all**
  groups awards the task's ``set_bonus_points`` (up to ``set_bonus_max``) — so
  Barrows pays +4 per brother and +40 for all six.
- **Batched decay.** Each item's points decay every ``awards_per_tier`` receipts
  instead of every receipt: Brimstone with ``awards_per_tier: 3`` pays full for
  the first 3, the 20%-step for the next 3, and so on (the sheet's duplicate
  rows). Default 1 = the normal 100/80/60/40/20.

Scoring is a **pure function of the applied ledger** (recomputed from every
surviving receipt), so apply/revoke are simple deltas and can never drift. No
I/O here — it stays unit-testable (the pytest conftest stubs ``db``/``services``).

Wiring: matcher/apply/revoke in :mod:`services.event_engine`; write-time config
validation in :mod:`web_api.routes.event_task_validation`. See docs/LOOT_SWEEP.md.
"""
from __future__ import annotations

import json
import math
from typing import Iterable, Optional

# ---- config defaults (mirrored by the web validator) ----------------------
DEFAULT_DECAY_PERCENT = 20      # percentage points shed per decay tier
DEFAULT_DECAY_MODE = "linear"   # "linear" | "geometric"
DEFAULT_AWARDS_PER_TIER = 1     # receipts sharing each decay tier
DEFAULT_TIERS = 5               # the sheet's 100/80/60/40/20 columns
DEFAULT_SET_BONUS_MAX = 1       # times the whole-set bonus pays out per team
DEFAULT_GROUP_BONUS_MAX = 1     # times a group bonus pays out per team

DECAY_MODES = ("linear", "geometric")

# Bounds the write validator enforces (kept here so scoring and validation
# agree on what a legal config looks like).
MAX_GROUPS = 40
MAX_ITEMS = 400          # across the whole task
MIN_ITEM_POINTS = 1
MAX_ITEM_POINTS = 1_000_000
MAX_AWARDS_PER_TIER = 20
MAX_TOTAL_AWARDS = 200   # cap on an item's max_awards
MAX_BONUS_POINTS = 10_000_000
MAX_BONUS_MAX = 100
MAX_NPCS_PER_GROUP = 30
MAX_MATCH_NAMES = 10     # alternate names that credit the same item entry
MAX_REQUIRED = 100       # receipts a group can demand of one entry


def _norm(value) -> str:
    """Case-insensitive name key — identical to ``event_engine._norm`` so a
    ledger row's ``matched_target`` / a drop's ``npc_name`` line up with config
    item / NPC names."""
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


def _clamp(value, default: int, lo: int, hi: int) -> int:
    n = _int(value, default)
    if value is None:
        n = default
    return min(max(n, lo), hi)


# --------------------------------------------------------------------------- #
# Award math
# --------------------------------------------------------------------------- #
def receipt_factor(k: int, decay_percent: int, awards_per_tier: int = 1,
                   decay_mode: str = DEFAULT_DECAY_MODE) -> float:
    """Multiplier on an item's base points for its ``k``-th receipt (1-indexed).

    Receipts are grouped into decay *tiers* of ``awards_per_tier`` each: with
    ``awards_per_tier=1`` every receipt steps down (100/80/60/40/20 for 20%);
    with ``awards_per_tier=3`` receipts 1-3 are full, 4-6 take the first step,
    etc. ``linear`` sheds ``decay_percent`` points per tier; ``geometric``
    multiplies by ``(1 − decay_percent/100)`` per tier. Never negative."""
    if k < 1:
        return 0.0
    apt = max(int(awards_per_tier or 1), 1)
    tier = (k - 1) // apt  # 0-indexed decay tier
    if decay_mode == "geometric":
        return max(0.0, (1.0 - decay_percent / 100.0) ** tier)
    return max(0.0, 1.0 - tier * decay_percent / 100.0)


def default_max_awards(awards_per_tier: int = 1) -> int:
    """Total scoring receipts when an item doesn't cap itself: the sheet's 5
    decay tiers × the per-tier batch size."""
    return DEFAULT_TIERS * max(int(awards_per_tier or 1), 1)


def _round2(value: float) -> float:
    """Scores are decimal-valued to 2 places (a 1-pointer's second receipt at
    20% decay is exactly 0.8, not 1). Rounding happens per receipt AND per
    aggregate so sums re-folded from the ledger can never drift."""
    return round(value + 0.0, 2)


def receipt_points(base: float, k: int, decay_percent: int,
                   awards_per_tier: int = 1,
                   decay_mode: str = DEFAULT_DECAY_MODE) -> float:
    """Points the ``k``-th receipt of an item is worth, to 2 decimals."""
    return _round2(base * receipt_factor(k, decay_percent, awards_per_tier, decay_mode))


def item_points(base: float, count: int, max_awards: int, decay_percent: int,
                awards_per_tier: int = 1, decay_mode: str = DEFAULT_DECAY_MODE) -> float:
    """Total points an item is worth to a team that received it ``count`` times:
    the first ``min(count, max_awards)`` receipts, each rounded to 2 decimals
    (so 1 pt at −20%/tier pays 1, 0.8, 0.6, …), tiered by ``awards_per_tier``."""
    scored = min(max(count, 0), max(max_awards, 0))
    total = 0.0
    for k in range(1, scored + 1):
        total += receipt_points(base, k, decay_percent, awards_per_tier, decay_mode)
    return _round2(total)


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #
ITEM_SOURCES = ("drop", "pet")


class LootSweepItem:
    __slots__ = ("key", "name", "item_id", "points", "awards_per_tier",
                 "max_awards", "counts_for_group", "source",
                 "match_names", "match_keys", "required", "virtual")

    def __init__(self, raw, cfg: "LootSweepConfig"):
        if isinstance(raw, str):
            name, entry = raw, {}
        else:
            entry = raw if isinstance(raw, dict) else {}
            name = entry.get("item_name") or entry.get("name") or ""
        self.key = _norm(name)
        self.name = str(name).strip()
        self.item_id = _int(entry.get("item_id"), 0) or None
        # A "virtual" entry's name is a custom LABEL ("Any ancestral piece"),
        # not itself a droppable item — the real items live in match_names, so
        # the label key never credits a receipt. Set explicitly by the write
        # validator (never inferred: a real item may also carry match_names).
        self.virtual = bool(entry.get("virtual"))
        pts = _num(entry.get("points"), 1.0)
        self.points = pts if pts > 0 else 1.0
        self.awards_per_tier = _clamp(entry.get("awards_per_tier"),
                                      DEFAULT_AWARDS_PER_TIER, 1, MAX_AWARDS_PER_TIER)
        mx = entry.get("max_awards")
        self.max_awards = (_clamp(mx, default_max_awards(self.awards_per_tier), 1, MAX_TOTAL_AWARDS)
                           if mx is not None else default_max_awards(self.awards_per_tier))
        # False = scores but doesn't gate its group's bonus (pets, mega-rares).
        self.counts_for_group = entry.get("counts_for_group", True) is not False
        # How the item is credited: "drop" (NPC-scoped drop, the default) or
        # "pet" (a `pet` submission matched by name — a pet only comes from its
        # boss, so no NPC scoping is needed).
        self.source = entry.get("source") if entry.get("source") in ITEM_SOURCES else "drop"
        # "Also counts as": alternate drop names that credit THIS entry's
        # counter/decay/cap — the vestige + gold-ring case, or an "any
        # ancestral piece" pool where one entry lists every piece. For a
        # virtual entry these ARE the pool (the label itself never matches).
        raw_aliases = entry.get("match_names") or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        names: list[str] = []
        keys: list[str] = [] if self.virtual else [self.key]
        for a in (raw_aliases if isinstance(raw_aliases, list) else [])[:MAX_MATCH_NAMES]:
            ak = _norm(a)
            if ak and ak not in keys:
                keys.append(ak)
                names.append(str(a).strip())
        self.match_names = names
        self.match_keys = tuple(keys)
        # Receipts (across ALL match names) the group demands before this entry
        # counts toward its completion — "any 3 ancestral pieces". Default 1.
        self.required = _clamp(entry.get("required"), 1, 1, MAX_REQUIRED)


class LootSweepGroup:
    """One sub-set: items tied to source NPC(s), with its own completion bonus."""

    __slots__ = ("label", "npcs", "npc_keys", "bonus_points", "bonus_max",
                 "items", "by_key", "image_url")

    def __init__(self, raw, cfg: "LootSweepConfig"):
        raw = raw if isinstance(raw, dict) else {}
        self.label = str(raw.get("label") or "").strip()
        npcs = raw.get("npcs") or raw.get("npc") or []
        if isinstance(npcs, str):
            npcs = [npcs]
        self.npcs = [str(n).strip() for n in npcs if str(n).strip()]
        self.npc_keys = frozenset(_norm(n) for n in self.npcs)
        self.bonus_points = max(_int(raw.get("bonus_points"), 0), 0)
        self.bonus_max = _clamp(raw.get("bonus_max"), DEFAULT_GROUP_BONUS_MAX, 1, MAX_BONUS_MAX)
        # Custom boss/category image URL (uploaded); None falls back to the
        # NPC's own artwork on the board.
        iu = raw.get("image_url")
        self.image_url = str(iu).strip()[:255] if iu and str(iu).strip() else None
        self.items: list[LootSweepItem] = []
        self.by_key: dict[str, LootSweepItem] = {}
        for it in (raw.get("items") or []):
            item = LootSweepItem(it, cfg)
            if not item.key or item.key in self.by_key:
                continue
            self.items.append(item)
            self.by_key[item.key] = item

    @property
    def set_item_keys(self) -> list[str]:
        return [it.key for it in self.items if it.counts_for_group]

    @property
    def gates(self) -> bool:
        """Does this group gate the whole-set bonus? (Has completion items and
        an active group bonus, i.e. it is a real sub-set not a bonus dumping
        ground.)"""
        return bool(self.set_item_keys)


class LootSweepConfig:
    """Normalized view of a loot_sweep task config (defaults resolved)."""

    __slots__ = ("decay_percent", "decay_mode", "set_bonus_points",
                 "set_bonus_max", "groups")

    def __init__(self, config):
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        config = config if isinstance(config, dict) else {}

        self.decay_percent = _clamp(config.get("decay_percent"), DEFAULT_DECAY_PERCENT, 0, 100)
        mode = config.get("decay_mode")
        self.decay_mode = mode if mode in DECAY_MODES else DEFAULT_DECAY_MODE
        self.set_bonus_points = max(_int(config.get("set_bonus_points"), 0), 0)
        self.set_bonus_max = _clamp(config.get("set_bonus_max"), DEFAULT_SET_BONUS_MAX, 1, MAX_BONUS_MAX)

        groups_raw = config.get("groups")
        if not groups_raw and config.get("items"):
            # v1 back-compat: a flat item list becomes a single group carrying
            # the set bonus (and any top-level npcs).
            groups_raw = [{
                "label": config.get("label") or "",
                "npcs": config.get("npcs") or [],
                "bonus_points": self.set_bonus_points,
                "bonus_max": self.set_bonus_max,
                "items": config.get("items"),
            }]
            self.set_bonus_points = 0
        self.groups: list[LootSweepGroup] = []
        for g in (groups_raw or []):
            grp = LootSweepGroup(g, self)
            if grp.items:
                self.groups.append(grp)

    def all_items(self) -> Iterable[LootSweepItem]:
        for g in self.groups:
            yield from g.items

    def matcher_index(self) -> dict[str, dict]:
        """``{match key: {"source", "npcs"}}`` for the engine matcher — every
        entry registers under its own name AND each of its ``match_names``
        aliases, so any listed name credits the entry. ``source`` is "drop"
        (NPC-scoped: ``npcs`` = allowed NPC keys, empty = any) or "pet"
        (matched from a pet submission by name; ``npcs`` unused). A key in two
        groups merges their NPC allowances (validation forbids that)."""
        out: dict[str, dict] = {}
        for g in self.groups:
            for it in g.items:
                for key in it.match_keys:
                    cur = out.get(key)
                    if cur is None:
                        out[key] = {"source": it.source,
                                    "npcs": g.npc_keys if it.source == "drop" else frozenset()}
                    else:
                        cur["npcs"] = cur["npcs"] | g.npc_keys
        return out


# --------------------------------------------------------------------------- #
# Team scoring (from ledger receipt counts)
# --------------------------------------------------------------------------- #
def counts_from_rows(rows: Iterable) -> dict[str, int]:
    """``{normalized item name: total receipts}`` from applied ledger rows.

    Each unit of ``quantity`` is one receipt. Bonus rows and rows without a
    ``matched_target`` are ignored. NPC scoping is enforced upstream (the
    matcher only records receipts from a valid NPC), so counts here are already
    clean."""
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

        {"total", "item_total", "group_bonus_total", "set_total",
         "set_completions", "set_awarded",
         "groups": [{"label", "npcs", "bonus_points", "bonus_max",
                     "completions", "awarded", "bonus_total", "item_total",
                     "items": [{item_id,name,key,count,scored,points,
                                max_awards,awards_per_tier,counts_for_group,
                                required,match_names}]}]}

    ``total`` is what the team's :class:`EventTeam` score should reflect for this
    task. Groups/items are in config order."""
    groups_out = []
    item_total_all = 0.0
    group_bonus_all = 0
    gating_completions: list[int] = []

    for g in config.groups:
        item_rows = []
        g_item_total = 0.0
        item_completions: list[int] = []
        for it in g.items:
            # An entry's receipts pool across ALL of its match names (aliases).
            count = sum(max(_int(counts.get(k), 0), 0) for k in it.match_keys)
            pts = item_points(it.points, count, it.max_awards, config.decay_percent,
                              it.awards_per_tier, config.decay_mode)
            g_item_total = _round2(g_item_total + pts)
            if it.counts_for_group:
                # "Collected" once every `required` receipts (any mix of the
                # entry's names) — an "any 3 ancestral pieces" entry completes
                # at 3, 6, 9… receipts for repeatable group bonuses.
                item_completions.append(count // it.required)
            item_rows.append({
                "item_id": it.item_id, "name": it.name, "key": it.key,
                "count": count, "scored": min(count, it.max_awards), "points": pts,
                "max_awards": it.max_awards, "awards_per_tier": it.awards_per_tier,
                "counts_for_group": it.counts_for_group, "source": it.source,
                "required": it.required, "match_names": it.match_names,
                "virtual": it.virtual,
            })
        completions = min(item_completions, default=0)
        has_gating = bool(item_completions)
        awarded = min(completions, g.bonus_max) if (has_gating and g.bonus_points > 0) else 0
        bonus_total = awarded * g.bonus_points
        item_total_all = _round2(item_total_all + g_item_total)
        group_bonus_all += bonus_total
        if g.gates:
            gating_completions.append(completions)
        groups_out.append({
            "label": g.label, "npcs": g.npcs,
            "bonus_points": g.bonus_points, "bonus_max": g.bonus_max,
            "completions": completions, "awarded": awarded, "bonus_total": bonus_total,
            "item_total": g_item_total, "items": item_rows,
        })

    # Whole-set bonus: how many times EVERY gating group has been completed.
    set_completions = min(gating_completions) if gating_completions else 0
    set_awarded = (min(set_completions, config.set_bonus_max)
                   if (config.set_bonus_points > 0 and gating_completions) else 0)
    set_total = set_awarded * config.set_bonus_points

    return {
        "total": _round2(item_total_all + group_bonus_all + set_total),
        "item_total": item_total_all,
        "group_bonus_total": group_bonus_all,
        "set_total": set_total,
        "set_completions": set_completions,
        "set_awarded": set_awarded,
        "groups": groups_out,
    }


def score_rows(rows: Iterable, config: LootSweepConfig) -> dict:
    """Convenience: :func:`score_counts` straight from ledger rows."""
    return score_counts(counts_from_rows(rows), config)


def team_total(rows: Iterable, config: LootSweepConfig) -> float:
    """Just the (2-decimal) team-score contribution for a (task, team)."""
    return score_rows(rows, config)["total"]
