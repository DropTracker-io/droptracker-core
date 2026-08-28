"""Bingo EHB — event-scoped effort, the pure core.

Contribution counters only ever record *credit*: a player who grinds a boss all
week and never gets the drop is invisible on every event surface. Effort fills
that gap by counting kills at the bosses an event's tasks actually care about,
whether or not anything dropped — Brondt's framing in the "Tracking effort
towards a drop" thread: 5-manning Venenatis all week is high global EHB and
zero bingo progress, and the reverse deserves to show.

Two ideas carry the feature:

**Relevance.** An event's effort NPCs are the union of its ``kc_target`` NPCs,
the source restrictions admins set on item tasks (``config.source_npcs`` /
``config.item_npcs``), and — when they set none — the NPCs that actually drop
the task's items. That last inference is what makes "Yama KC counts toward the
Oathplate tile" work with no admin setup.

**Freezing.** Effort at an NPC stops accruing once every task that NPC feeds is
complete for the team, so kills after the tile is done don't inflate the score.

This module is deliberately pure — no DB, no Redis, no network, stdlib-only
module imports — so it loads under the unit-test suite's stubbed ``services``
package and the scoring rules can be tested directly. Config parsing stays in
``services/event_engine.py`` (single source of truth); callers hand this module
already-extracted descriptors plus two injected lookups.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

# ── Caps ─────────────────────────────────────────────────────────────────────
# A single common item can name hundreds of source NPCs ("Uncut ruby" has 261),
# and the effort map is consulted per submission, so inference is bounded on
# both axes. Sources come back rarity-ranked, so the cap keeps the rarest —
# i.e. the most boss-like, which is what an event tile is actually about.
EFFORT_SOURCES_PER_ITEM = 8
#: Hard ceiling on one event's effort NPC set, after the union.
#:
#: Sized for a whole-content loot sweep, which legitimately spans every boss in
#: the game: the live "Loot Sweep (All Content)" event resolves 155 NPCs and was
#: silently truncated at the old ceiling of 80. The map is a dict consulted with
#: one lookup per submission, so its SIZE costs nothing on the hot path — the
#: ceiling only bounds the one-off inference at state load (~7s for that event,
#: then Redis-cached for hours) and the cached blob.
EFFORT_MAX_NPCS = 300

#: Redis scope prefix for effort KC watermarks. Distinct from every credit
#: scope (``_kc_state_scope`` uses bare task ids / ``{task}:{npc}``) so effort
#: folding can never consume a crediting task's watermark, or vice versa.
EFFORT_SCOPE_PREFIX = "eff"
#: Scope prefix for the COMPLETION counter at partial-credit NPCs — see
#: :data:`COMPLETION_MARKERS`. Separate from ``eff:`` because it counts a
#: different event, which is the entire point.
COMPLETION_SCOPE_PREFIX = "effc"

#: NPCs where the plugin's loot kill count and the WOM boss metric count
#: DIFFERENT events, because the content pays out for a partial attempt.
#:
#: The Colosseum is the case that forced this. You may leave after any wave and
#: keep a reward chest, so the plugin's chest KC counts *attempts*, while WOM's
#: ``sol_heredit`` only counts the wave-12 kill. Pricing an attempt at the
#: completion rate charged 22 minutes for a run that our own timings say takes
#: about two: consecutive-attempt gaps measured 2026-08-28 over 4,516 attempts
#: read a median 34.6 min between completions and 1.9 min between bails, and
#: **86% of all Colosseum attempts are bails**. One splinter farmer with 200
#: attempts and 5 completions booked 74 EHE hours against a true ~8.
#:
#: So a marker NPC keeps two counters: ``kills`` (attempts) and ``completions``
#: (the subset that reached the WOM-counted event), priced separately by
#: :func:`rows_to_summary`. ``item`` is the drop that proves a completion —
#: Sol Heredit awards exactly one uncharged quiver per kill, so counting those
#: envelopes counts completions.
#:
#: Adding an NPC here is a data decision, not a config one: it needs a drop
#: that is awarded once and only on completion, and a measured partial rate in
#: ``npc_ehb_rates`` (``scripts/compute_npc_ehb_rates.py --partials``).
COMPLETION_MARKERS = {
    "fortis colosseum": {
        "item": "dizana's quiver (uncharged)",
        "metric": "sol_heredit",
    },
}


def _norm(value) -> str:
    """Mirror of ``services.event_engine._norm`` — the engine compares NPC
    names this way (lower-cased, whitespace-collapsed), and effort lookups key
    off envelope NPC names the engine already normalized, so the two must
    agree. Deliberately not ``utils.npc_names.npc_match_key``: that folds
    aliases the engine's KC path does not."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def effort_scope(npc_norm: str) -> str:
    """Watermark scope for one NPC's effort counter, per (event, npc, player).

    Effort is NOT scoped per task: an NPC feeding three tiles is still one
    in-game kill counter, and three watermarks over one absolute KC would
    triple-count it.
    """
    return f"{EFFORT_SCOPE_PREFIX}:{_norm(npc_norm).replace(' ', '_')}"


def completion_scope(npc_norm: str) -> str:
    """Watermark scope for a marker NPC's COMPLETION counter.

    Deliberately a different scope from :func:`effort_scope`: the two count
    different events (attempts vs completions) and folding them together is the
    bug this whole mechanism exists to prevent.
    """
    return f"{COMPLETION_SCOPE_PREFIX}:{_norm(npc_norm).replace(' ', '_')}"


def completion_marker(npc_norm) -> Optional[dict]:
    """The :data:`COMPLETION_MARKERS` entry for an NPC, or ``None``."""
    return COMPLETION_MARKERS.get(_norm(npc_norm))


def is_completion_drop(npc_norm, item_name) -> bool:
    """Whether this drop proves a completion at a marker NPC."""
    marker = completion_marker(npc_norm)
    return bool(marker) and _norm(item_name) == marker["item"]


def build_effort_map(
    task_descriptors: Iterable[dict],
    *,
    resolve_sources: Callable[[list], dict],
    boss_metric: Callable[[str], Optional[str]],
    max_npcs: int = EFFORT_MAX_NPCS,
) -> dict:
    """``{normalized npc name: {"npc_id", "metric", "tasks": [task_id, ...]}}``.

    ``task_descriptors`` are ``{"task_id", "npcs": [normalized names],
    "item_names": [raw names], "npc_ids": {name: id}}`` — the engine extracts
    these from task configs so config parsing has one home. ``npcs`` are the
    explicit NPCs (a ``kc_target``'s set, or an item task's configured source
    restriction); ``item_names`` triggers inference and is expected to be empty
    when the admin already restricted sources.

    ``resolve_sources(item_names) -> {npc name: npc_id}`` is the injected
    DB-backed inference (``db.item_sources.source_npcs_for_item_names``);
    ``boss_metric(name) -> slug | None`` maps an NPC to its WOM metric
    (``utils.wiseoldman.wom_boss_metric``). An NPC with no WOM metric is still
    tracked — it earns activity (and so counts for the inactivity report) but
    contributes no EHB, since there's no rate to divide by.

    Explicit NPCs are added before inferred ones, so when the cap bites it is
    always the guesses that get dropped.
    """
    out: dict = {}

    def _add(name, npc_id, task_id) -> None:
        key = _norm(name)
        if not key:
            return
        entry = out.get(key)
        if entry is None:
            if len(out) >= max_npcs:
                return
            entry = {
                "npc_id": int(npc_id) if npc_id is not None else None,
                "metric": boss_metric(key) or None,
                "tasks": [],
            }
            out[key] = entry
        elif entry["npc_id"] is None and npc_id is not None:
            entry["npc_id"] = int(npc_id)
        if task_id not in entry["tasks"]:
            entry["tasks"].append(task_id)

    descriptors = list(task_descriptors)
    for d in descriptors:
        ids = d.get("npc_ids") or {}
        for name in (d.get("npcs") or []):
            _add(name, ids.get(_norm(name), ids.get(name)), d.get("task_id"))

    # Inference second — see the docstring's ordering note. Batched per task so
    # one query burst covers a whole tile's item list.
    for d in descriptors:
        names = list(d.get("item_names") or [])
        if not names:
            continue
        try:
            found = resolve_sources(names) or {}
        except Exception:
            # Inference is best-effort: a wiki/DB hiccup must degrade the
            # effort map, never break matcher state loading.
            continue
        for npc_name, npc_id in found.items():
            _add(npc_name, npc_id, d.get("task_id"))
    return out


def ehb_hours(kills_by_metric: dict, rates: dict) -> float:
    """Efficient hours bossed for ``{wom metric slug: kills}`` at ``rates``
    (``{slug: kills per hour}``).

    Metrics with no rate contribute 0 — WOM publishes rates for ~66 bosses, and
    everything else (skilling activities, brand-new content, chest/collective
    sources) is real activity we simply cannot price. Returning 0 rather than
    guessing keeps the number honest.
    """
    total = 0.0
    for metric, kills in (kills_by_metric or {}).items():
        try:
            rate = float((rates or {}).get(metric) or 0)
            count = float(kills or 0)
        except (TypeError, ValueError):
            continue
        if rate > 0 and count > 0:
            total += count / rate
    return total


def _derived_rate(derived_rates: Optional[dict], npc_id) -> float:
    """The usable derived kills/hour for an NPC id, or 0. Tolerates junk on
    both sides (string ids from JSON, non-numeric rates)."""
    if not derived_rates or npc_id is None:
        return 0.0
    try:
        rate = float(derived_rates.get(int(npc_id)) or 0)
    except (TypeError, ValueError):
        return 0.0
    return rate if rate > 0 else 0.0


def _price_marker_row(row, kills: int, rates: dict,
                      derived_rates: Optional[dict]) -> Optional[tuple]:
    """``(hours, estimated_hours)`` for a :data:`COMPLETION_MARKERS` NPC, or
    ``None`` when this row is not one.

    Completions are priced at the WOM rate for the metric they actually earn;
    everything else is a partial attempt, priced at our measured attempt rate
    from ``npc_ehb_rates``. Charging a bail the completion rate is what put a
    player at 329 EHE hours on 2026-08-28.

    ``completions`` may legitimately exceed ``kills``: WOM sees completions for
    a player whose plugin never reported an attempt, so the attempt total is
    ``max`` of the two rather than the plugin's alone.
    """
    marker = completion_marker(row.get("npc_name"))
    if marker is None:
        return None
    try:
        completions = max(0, int(row.get("completions") or 0))
    except (TypeError, ValueError):
        completions = 0
    attempts = max(kills, completions)
    completions = min(completions, attempts)
    partials = attempts - completions

    wom_rate = 0.0
    try:
        wom_rate = float((rates or {}).get(row.get("boss_metric")) or 0)
    except (TypeError, ValueError):
        wom_rate = 0.0
    partial_rate = _derived_rate(derived_rates, row.get("npc_id"))

    hours = (completions / wom_rate) if (wom_rate > 0 and completions) else 0.0
    # A partial with no measured rate contributes 0 — the honest zero, and far
    # closer to the truth than a completion's worth of hours.
    estimated = (partials / partial_rate) if (partial_rate > 0 and partials) else 0.0
    return hours + estimated, estimated


def rows_to_summary(rows: Iterable[dict], rates: dict,
                    derived_rates: Optional[dict] = None) -> dict:
    """Fold one player's effort rows into the shape the read model exposes.

    ``rows`` are ``{"npc_id", "npc_name", "boss_metric", "kills", "completions",
    "last_at", "frozen_at"}``. Returns ``{"ehb_hours", "ehb_estimated_hours",
    "kills",
    "bosses": [...], "last_at", "frozen"}`` with bosses ordered by EHB
    contribution (then kills), so the biggest investment reads first.

    ``derived_rates`` is ``{npc_id: kills per hour}`` from ``npc_ehb_rates`` —
    our own estimates for bosses WOM publishes no rate for (thread #93:
    approximation is fine, but labelled). Pricing order per row is strict:

    1. The WOM rate for the row's metric, when published. A derived rate never
       overrides a WOM one, even when both exist.
    2. The derived rate for the row's ``npc_id``. Hours priced this way are
       flagged ``estimated`` on the boss entry and summed separately into
       ``ehb_estimated_hours`` (a subset of ``ehb_hours``, not an addition).
    3. Nothing — the honest 0 this feature started with.

    :data:`COMPLETION_MARKERS` NPCs take a different path entirely
    (:func:`_price_marker_row`): their two counters are priced separately,
    because for those the WOM rate answers a question the plugin's counter is
    not asking.
    """
    bosses, total_kills = [], 0
    total_hours = 0.0
    estimated_hours = 0.0
    last_at = None
    frozen = 0
    for row in rows or []:
        try:
            kills = int(row.get("kills") or 0)
        except (TypeError, ValueError):
            kills = 0
        metric = row.get("boss_metric") or None
        marker_priced = _price_marker_row(row, kills, rates, derived_rates)
        if marker_priced is not None:
            # A marker row's attempt total is max(plugin attempts, completions):
            # WOM can see completions the plugin never reported.
            try:
                kills = max(kills, int(row.get("completions") or 0))
            except (TypeError, ValueError):
                pass
        if kills <= 0:
            continue
        total_kills += kills
        row_last = row.get("last_at")
        if row_last is not None and (last_at is None or row_last > last_at):
            last_at = row_last
        is_frozen = row.get("frozen_at") is not None
        if is_frozen:
            frozen += 1
        if marker_priced is not None:
            hours, row_estimated_hours = marker_priced
            estimated = row_estimated_hours > 0
        else:
            hours = ehb_hours({metric: kills}, rates) if metric else 0.0
            estimated = False
            if hours <= 0:
                derived = _derived_rate(derived_rates, row.get("npc_id"))
                if derived > 0:
                    hours = kills / derived
                    estimated = True
            row_estimated_hours = hours if estimated else 0.0
        total_hours += hours
        estimated_hours += row_estimated_hours
        bosses.append(
            {
                "npc_id": row.get("npc_id"),
                "name": row.get("npc_name"),
                "metric": metric,
                "kills": kills,
                "ehb_hours": hours,
                "estimated": estimated,
                "frozen": is_frozen,
            }
        )
    bosses.sort(key=lambda b: (-b["ehb_hours"], -b["kills"], str(b["name"] or "")))
    return {
        "ehb_hours": total_hours,
        "ehb_estimated_hours": estimated_hours,
        "kills": total_kills,
        "bosses": bosses,
        "last_at": last_at,
        "frozen": frozen,
    }
