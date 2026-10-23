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
# A set/section completion scores like an item receipt: it pays full value the
# first time the set is completed and DECAYS on each successive completion
# (40 → 32 → 24 … at 20%), up to this many completions. Default = the item
# decay-tier count so a linear-decay bonus fades to zero exactly like an item.
DEFAULT_SET_BONUS_MAX = DEFAULT_TIERS     # decaying completions of the whole-set bonus
DEFAULT_GROUP_BONUS_MAX = DEFAULT_TIERS   # decaying completions of a group (sub-set) bonus

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
                 "match_names", "match_keys", "required", "virtual", "per_drop")

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
        # "Count each drop once": a stack (3 Hueycoatl hides, a pile of echo
        # crystals) is one receipt instead of one per unit (utils.per_drop).
        self.per_drop = entry.get("per_drop") is True
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
                 "set_bonus_max", "groups", "_groups_by_key")

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
        # match key -> indices of the groups holding an entry for it. Usually
        # one; several when an item repeats across NPC-disjoint groups (Dragon
        # 2h under Chaos Elemental AND Venenatis), where the receipt's NPC
        # picks the group — see :meth:`route`.
        self._groups_by_key: dict[str, list[int]] = {}
        for gi, grp in enumerate(self.groups):
            for it in grp.items:
                for k in it.match_keys:
                    lst = self._groups_by_key.setdefault(k, [])
                    if gi not in lst:
                        lst.append(gi)

    def all_items(self) -> Iterable[LootSweepItem]:
        for g in self.groups:
            yield from g.items

    def route(self, matched_target, matched_npc=None) -> Optional[int]:
        """Index of the group a receipt of ``matched_target`` credits, or
        ``None`` when the name isn't part of this task.

        An item may repeat across groups only when their NPC lists are
        disjoint (the write validator enforces it), so the NPC that dropped it
        (``matched_npc``, stored on the ledger row) names exactly one group.
        A row without a usable NPC — older rows from before the column
        existed, pets, manual awards that didn't pick a boss — falls back to
        the first group listing the name. Older rows are always unambiguous:
        their configs could never repeat a name across groups."""
        cands = self._groups_by_key.get(_norm(matched_target))
        if not cands:
            return None
        if len(cands) > 1:
            npc = _norm(matched_npc)
            if npc:
                for gi in cands:
                    if npc in self.groups[gi].npc_keys:
                        return gi
        return cands[0]

    def is_shared(self, matched_target) -> bool:
        """Does this name credit entries in more than one group?"""
        return len(self._groups_by_key.get(_norm(matched_target)) or ()) > 1

    def matcher_index(self) -> dict[str, dict]:
        """``{match key: {"source", "npcs"}}`` for the engine matcher — every
        entry registers under its own name AND each of its ``match_names``
        aliases, so any listed name credits the entry. ``source`` is "drop"
        (NPC-scoped: ``npcs`` = allowed NPC keys, empty = any) or "pet"
        (matched from a pet submission by name; ``npcs`` unused). ``per_drop``
        is set when an entry holding the key counts each drop once (a stack is
        one receipt); if several entries hold the key, any one flagging it
        applies to the drop. A key in two
        groups merges their NPC allowances: the drop qualifies if ANY of its
        groups takes that NPC, and :meth:`route` then picks the group. (The
        validator only lets a name repeat across groups that all name NPCs, so
        the union never widens into "any NPC".)"""
        out: dict[str, dict] = {}
        for g in self.groups:
            for it in g.items:
                for key in it.match_keys:
                    cur = out.get(key)
                    if cur is None:
                        out[key] = {"source": it.source,
                                    "npcs": g.npc_keys if it.source == "drop" else frozenset(),
                                    "per_drop": it.per_drop}
                    else:
                        cur["npcs"] = cur["npcs"] | g.npc_keys
                        cur["per_drop"] = cur["per_drop"] or it.per_drop
        return out


# --------------------------------------------------------------------------- #
# Team scoring (from ledger receipt counts)
# --------------------------------------------------------------------------- #
def counts_from_rows(rows: Iterable,
                     config: Optional["LootSweepConfig"] = None) -> dict:
    """Receipt counts from applied ledger rows.

    Without ``config``: ``{normalized item name: total receipts}``. With it,
    each row is routed to its group (:meth:`LootSweepConfig.route`, by the
    row's ``matched_npc``) and the result is ``{(group index, name): total}``
    — needed once a name repeats across groups, so a Chaos Elemental Dragon 2h
    never counts toward Venenatis. :func:`score_counts` reads either shape.

    Each unit of ``quantity`` is one receipt. Bonus rows and rows without a
    ``matched_target`` are ignored. NPC scoping is enforced upstream (the
    matcher only records receipts from a valid NPC), so counts here are already
    clean."""
    counts: dict = {}
    for r in rows:
        if (getattr(r, "source_type", None) or "") == "bonus":
            continue
        key = _norm(getattr(r, "matched_target", None))
        if not key:
            continue
        if config is not None:
            gi = config.route(key, getattr(r, "matched_npc", None))
            if gi is None:
                continue
            ck = (gi, key)
        else:
            ck = key
        counts[ck] = counts.get(ck, 0) + max(_int(getattr(r, "quantity", 1), 1), 1)
    return counts


def _count(counts: dict, gi: int, key: str) -> int:
    """One entry name's receipts in group ``gi`` — group-routed counts first
    (``(gi, key)``), else the plain name-keyed shape."""
    if (gi, key) in counts:
        return max(_int(counts.get((gi, key)), 0), 0)
    return max(_int(counts.get(key), 0), 0)


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
    group_bonus_all = 0.0
    gating_completions: list[int] = []

    for gi, g in enumerate(config.groups):
        item_rows = []
        g_item_total = 0.0
        item_completions: list[int] = []
        for it in g.items:
            # An entry's receipts pool across ALL of its match names (aliases).
            count = sum(_count(counts, gi, k) for k in it.match_keys)
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
                "virtual": it.virtual, "per_drop": it.per_drop,
            })
        completions = min(item_completions, default=0)
        has_gating = bool(item_completions)
        # A set (sub-)completion scores like an item receipt: full value the
        # first time the team holds one of each gating item, then DECAYING on
        # each successive completion (a 4-pt sub-set pays 4, 3.2, 2.4 … at 20%),
        # up to ``bonus_max`` completions. Mirrors item decay so "complete the
        # set again" behaves exactly like "receive the item again".
        if has_gating and g.bonus_points > 0:
            bonus_total = item_points(g.bonus_points, completions, g.bonus_max,
                                      config.decay_percent, 1, config.decay_mode)
            awarded = min(completions, g.bonus_max)
        else:
            bonus_total = 0.0
            awarded = 0
        item_total_all = _round2(item_total_all + g_item_total)
        group_bonus_all = _round2(group_bonus_all + bonus_total)
        if g.gates:
            gating_completions.append(completions)
        groups_out.append({
            "label": g.label, "npcs": g.npcs,
            "bonus_points": g.bonus_points, "bonus_max": g.bonus_max,
            "completions": completions, "awarded": awarded, "bonus_total": bonus_total,
            "item_total": g_item_total, "items": item_rows,
        })

    # Whole-set bonus: how many times EVERY gating group has been completed.
    # Like the group bonus, each whole-set completion decays (40 → 32 → … at
    # 20%), up to ``set_bonus_max`` completions.
    set_completions = min(gating_completions) if gating_completions else 0
    if config.set_bonus_points > 0 and gating_completions:
        set_total = item_points(config.set_bonus_points, set_completions,
                                config.set_bonus_max, config.decay_percent, 1,
                                config.decay_mode)
        set_awarded = min(set_completions, config.set_bonus_max)
    else:
        set_total = 0.0
        set_awarded = 0

    return {
        "total": _round2(item_total_all + group_bonus_all + set_total),
        "item_total": item_total_all,
        "group_bonus_total": group_bonus_all,
        "set_total": set_total,
        "set_completions": set_completions,
        "set_awarded": set_awarded,
        "groups": groups_out,
    }


def icon_ids_for(item: "LootSweepItem", alias_id_by_key: dict) -> list:
    """Ordered, de-duped display icons for an entry: the primary item id (real
    items only — a virtual label has none) then each match name's id, looked up
    in ``alias_id_by_key`` (keyed by :func:`_norm` of the name). Used by the
    board + receipts endpoints so a pooled/virtual entry shows every piece it
    stands for. Lives here (beside ``_norm``) so it's importable + unit-tested
    rather than duplicated as an endpoint closure."""
    ids: list = []
    if item.item_id and not item.virtual:
        ids.append(int(item.item_id))
    for a in item.match_names:
        aid = alias_id_by_key.get(_norm(a))
        if aid and int(aid) not in ids:
            ids.append(int(aid))
    return ids


def score_rows(rows: Iterable, config: LootSweepConfig) -> dict:
    """Convenience: :func:`score_counts` straight from ledger rows."""
    return score_counts(counts_from_rows(rows, config), config)


def team_total(rows: Iterable, config: LootSweepConfig) -> float:
    """Just the (2-decimal) team-score contribution for a (task, team)."""
    return score_rows(rows, config)["total"]


def receipt_points_by_row(rows: Iterable, config: LootSweepConfig) -> dict:
    """``{row.id: item receipt points}`` for a task's applied ledger rows in
    credit order — the same incremental ``item_points`` fold the board scores
    with and the per-item receipts card shows, generalized across every item.

    Cumulative counts are tracked per ``(team_id, group, item)`` and pool
    across an item's aliases (``match_keys``), so the n-th receipt decays
    correctly no matter which alias dropped; a name repeated across groups is
    routed by the row's NPC (:meth:`LootSweepConfig.route`). Only per-item
    receipt points are attributed —
    group/set bonuses arise from combinations across rows and can't be credited
    to a single receipt. ``rows`` MUST be ordered oldest-first."""
    item_by_key: dict = {}
    for gi, g in enumerate(config.groups):
        for it in g.items:
            for k in it.match_keys:
                item_by_key[(gi, k)] = it
    cum: dict = {}
    out: dict = {}
    for r in rows:
        rid = getattr(r, "id", None)
        if (getattr(r, "source_type", None) or "") == "bonus":
            out[rid] = 0.0
            continue
        key = _norm(getattr(r, "matched_target", None))
        gi = config.route(key, getattr(r, "matched_npc", None))
        it = item_by_key.get((gi, key)) if gi is not None else None
        team_id = getattr(r, "team_id", None)
        if it is None or team_id is None:
            out[rid] = 0.0
            continue
        qty = max(_int(getattr(r, "quantity", 1), 1), 1)
        ck = (team_id, gi, it.key)
        before = cum.get(ck, 0)
        after = before + qty
        cum[ck] = after
        out[rid] = _round2(
            item_points(it.points, after, it.max_awards, config.decay_percent,
                        it.awards_per_tier, config.decay_mode)
            - item_points(it.points, before, it.max_awards, config.decay_percent,
                          it.awards_per_tier, config.decay_mode)
        )
    return out


def _locate_entry(config: LootSweepConfig, matched_target,
                  matched_npc=None) -> Optional[tuple]:
    """``(group_index, item_index, LootSweepItem, LootSweepGroup)`` for the
    config entry a receipt of ``matched_target`` credits (matching the entry's
    own name OR any of its ``match_names`` aliases), or ``None`` when the drop
    isn't part of this task. A name in several groups is routed by
    ``matched_npc``. Indices line up with the config-ordered
    :func:`score_counts` ``groups``/``items`` so a caller can read the matching
    breakdown rows by position."""
    mk = _norm(matched_target)
    if not mk:
        return None
    gi = config.route(mk, matched_npc)
    if gi is None:
        return None
    g = config.groups[gi]
    for ii, it in enumerate(g.items):
        if mk in it.match_keys:
            return gi, ii, it, g
    return None


def receipt_detail(curr: dict, prev: dict, config: LootSweepConfig,
                   matched_target, matched_npc=None) -> Optional[dict]:
    """Per-receipt enrichment for a Loot Sweep Discord message — everything a
    reader needs about a single scoring drop without leaving Discord:

        {"item_name", "item_id", "npcs", "group_label",
         "item_count", "item_scored", "item_max", "item_remaining",
         "item_base_points", "received_points", "next_receipt_points",
         "group_have", "group_need", "group_completions", "group_awarded",
         "group_bonus_points",
         "set_have", "set_need", "set_completions", "set_awarded",
         "set_bonus_points"}

    ``curr`` folds the receipt in; ``prev`` excludes it (both from
    :func:`score_counts`), so ``received_points`` is exactly what THIS receipt
    added to the item's decayed total. ``item_remaining`` is how many more copies
    can still score (``max_awards − count``, floored at 0); ``next_receipt_points``
    is what the next copy would be worth (0 once capped). Group/set ``have/need``
    count gating entries collected vs. required. Pure (reads only the breakdown +
    config), so it stays unit-testable under the conftest stubs. ``None`` when the
    drop isn't a config item (caller drops the enrichment)."""
    located = _locate_entry(config, matched_target, matched_npc)
    if located is None:
        return None
    gi, ii, item, group = located
    try:
        cg = curr["groups"][gi]
        ci = cg["items"][ii]
    except (KeyError, IndexError, TypeError):
        return None
    pi = None
    try:
        pi = prev["groups"][gi]["items"][ii]
    except (KeyError, IndexError, TypeError):
        pi = None

    item_count = _int(ci.get("count"), 0)
    item_max = _int(ci.get("max_awards"), item.max_awards)
    prev_points = _num((pi or {}).get("points"), 0.0)
    received_points = _round2(_num(ci.get("points"), 0.0) - prev_points)
    next_receipt_points = (
        receipt_points(item.points, item_count + 1, config.decay_percent,
                       item.awards_per_tier, config.decay_mode)
        if item_count < item_max else 0.0)

    # Group progress: gating entries (counts_for_group) collected at least once
    # (count >= required) vs. the group's gating-entry total.
    gating = [bi for bi in cg.get("items") or [] if bi.get("counts_for_group")]
    group_have = sum(1 for bi in gating
                     if _int(bi.get("count"), 0) >= max(_int(bi.get("required"), 1), 1))

    # Set progress: gating groups (those with completion items) that have been
    # completed at least once, vs. the count of gating groups.
    set_need = set_have = 0
    for j, g in enumerate(config.groups):
        if not g.gates:
            continue
        set_need += 1
        try:
            if _int(curr["groups"][j].get("completions"), 0) >= 1:
                set_have += 1
        except (KeyError, IndexError, TypeError):
            pass

    return {
        "item_name": item.name,
        "item_id": item.item_id,
        "npcs": list(group.npcs),
        "group_image": group.image_url,
        "group_label": group.label or (group.npcs[0] if group.npcs else None),
        "item_count": item_count,
        "item_scored": _int(ci.get("scored"), min(item_count, item_max)),
        "item_max": item_max,
        "item_remaining": max(item_max - item_count, 0),
        "item_base_points": item.points,
        "received_points": received_points,
        "next_receipt_points": next_receipt_points,
        "group_have": group_have,
        "group_need": len(gating),
        "group_completions": _int(cg.get("completions"), 0),
        "group_awarded": _int(cg.get("awarded"), 0),
        "group_bonus_points": group.bonus_points,
        "set_have": set_have,
        "set_need": set_need,
        "set_completions": _int(curr.get("set_completions"), 0),
        "set_awarded": _int(curr.get("set_awarded"), 0),
        "set_bonus_points": config.set_bonus_points,
    }


# --------------------------------------------------------------------------- #
# DT2 vestige chains
# --------------------------------------------------------------------------- #
# Each ancient vestige takes three successful rolls at its boss: the first
# drops 1 Gold ring, the second 2 Gold rings, the third the vestige. An entry
# that lists a vestige AND names Gold ring under "also counts as" treats the
# three drops as one acquisition: rings from that vestige's boss belong to its
# chain, and the whole chain is ONE receipt. The drop says where it sits in
# the chain (1 ring = step 1, 2 rings = step 2, vestige = step 3), so a step
# credits only when it starts a new chain: the player's last credited step for
# that vestige is the same step or a later one (or there is none). A second
# full chain by the same player credits again.
VESTIGE_CHAIN_STEPS = 3
_CHAIN_NOTE_PREFIX = "vestige:"


def _vestige_rings():
    # Lazy: utils.vestige_rings is stdlib-only, but this module must stay
    # importable on its own (tests load it straight from the file).
    from utils import vestige_rings
    return vestige_rings


def vestige_chain_sighting(config: "LootSweepConfig", item_name, npc_name,
                           quantity=1) -> Optional[tuple]:
    """``(vestige name, step)`` when this drop is a step of a vestige chain
    for this task, else ``None`` (an ordinary receipt).

    Only applies to an entry that holds both the vestige and Gold ring among
    its names, in the group the drop routes to, and only for the vestige's
    own boss: a Gold ring from anywhere else stays an ordinary alias receipt.
    """
    vr = _vestige_rings()
    key = _norm(item_name)
    is_ring = key == vr.RING_NAME
    if not is_ring and not vr.is_vestige(key):
        return None
    gi = config.route(key, npc_name)
    if gi is None:
        return None
    npc = _norm(npc_name)
    for it in config.groups[gi].items:
        if key not in it.match_keys or vr.RING_NAME not in it.match_keys:
            continue
        for vname, bosses in vr.VESTIGE_BOSSES.items():
            vkey = _norm(vname)
            if vkey not in it.match_keys or npc not in bosses:
                continue
            if not is_ring and vkey != key:
                continue
            if not is_ring:
                return vname, VESTIGE_CHAIN_STEPS
            return vname, (1 if max(_int(quantity, 1), 1) == 1 else 2)
    return None


def vestige_chain_note(vestige: str, step: int) -> str:
    """Ledger note for a credited chain step: a parseable tag plus a human
    half for the admin ledger (``vestige:2:ultor vestige | step 2 of 3``)."""
    return (f"{_CHAIN_NOTE_PREFIX}{int(step)}:{_norm(vestige)} | "
            f"{vestige} chain, step {int(step)} of {VESTIGE_CHAIN_STEPS}")


def parse_vestige_chain_note(note) -> Optional[tuple]:
    """``(normalized vestige, step)`` from a chain note, else ``None``."""
    s = str(note or "")
    if not s.startswith(_CHAIN_NOTE_PREFIX):
        return None
    tag = s.split("|", 1)[0].strip()[len(_CHAIN_NOTE_PREFIX):]
    step, _, vestige = tag.partition(":")
    try:
        step_n = int(step)
    except ValueError:
        return None
    return (vestige.strip(), step_n) if vestige.strip() else None


def chain_step_credits(last_step: Optional[int], step: int) -> bool:
    """Does this step start a new chain? Yes when the player has no credited
    step for the vestige, or the last one was this step or later (that chain
    is over). An earlier last step means this drop continues that chain."""
    return last_step is None or last_step >= step
