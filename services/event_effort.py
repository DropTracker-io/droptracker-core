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

**Pairing.** One "kill" in OSRS can be banked: a clue casket. Clue tiers were
excluded from EHE entirely until a player's openings could be checked against
what they were actually dealt during the window — see :data:`CLUE_TIERS`.

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
#: Scope prefix for the ROLL counter at clue-tier NPCs — see :data:`CLUE_TIERS`.
#: A roll is not an attempt at the clue and not a completion of one; it happens
#: at a different NPC entirely (the monster that dropped the scroll), so it gets
#: its own scope for the same reason ``effc:`` does.
ROLL_SCOPE_PREFIX = "effr"

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


#: Item-name prefixes that all mean "a clue scroll of this tier was rolled".
#:
#: The tier's own scroll is the *rare* case: monsters overwhelmingly drop a
#: **Scroll box**, and matching only "Clue scroll (tier)" would have zeroed
#: every tier but elite. Measured over one week of prod drops: 3,703 hard
#: scroll boxes against 6 hard clue scrolls, 592 master boxes against 1 master
#: scroll. The remaining three are the skilling sources (fishing, bird nests,
#: mining), which the loot tracker sees only occasionally — included because
#: they are free to match, not because they carry much weight.
#:
#: ``Reward casket (tier)`` is deliberately absent: a casket is the *reward* for
#: a clue already finished, so counting one as a roll would pay for the same
#: clue twice.
CLUE_SOURCE_PREFIXES = (
    "scroll box", "clue scroll", "clue bottle", "clue nest", "clue geode",
)

#: Clue tiers, keyed by the pseudo-NPC ``npc_list`` records a casket under
#: (``osrs_api.semantic`` maps "Reward casket (elite)" -> "Clue Scroll (Elite)",
#: and the plugin's ``kill_count`` on those drops is the player's absolute
#: clue-completion count for the tier).
#:
#: **Why these NPCs need a second counter.** A casket is the one "kill" in OSRS
#: a player can bank. Clues were excluded from EHE for exactly that reason
#: (suggestion #156): a stack of 100 elites saved up beforehand and dumped in
#: the first five minutes would book a hundred clues of hours the event never
#: cost. The fear is not hypothetical — across 30 days of prod openings, 47% to
#: 76% of a player's consecutive openings per tier land under 30 seconds apart.
#:
#: So each tier keeps two counters: ``kills`` (caskets opened in-window) and
#: ``rolls`` (scrolls the player was *dealt* in-window, from any source), and
#: :func:`rows_to_summary` prices ``min(rolls, kills)`` — Fazebook's two-part
#: check: "if 20 clues are rolled and 25 clues opened, count the first 20".
#: A dumped stack still shows its openings; it just isn't paid for.
#:
#: ``rate_kph`` is clues completed per hour. WOM publishes no EHB rate for clue
#: metrics, so these are DropTracker's own and every hour priced from them is
#: flagged ``estimated``. Measured 2026-08-30 as the median per-player median
#: gap between consecutive casket openings over 30 days, gaps bounded to
#: [30s, 30min] so stack dumps and logouts fall out. MEDIAN across players, not
#: the p90 ``scripts/compute_npc_ehb_rates.py`` uses for bosses: there the tail
#: is efficient play, here it is precisely the bulk-opening this mechanism
#: exists to refuse to pay for. (Easy edging out beginner is what the data
#: says — 2.3 vs 2.2 min medians — and is inside the noise of both samples.)
#: A row in ``npc_ehb_rates`` for the pseudo-NPC overrides the built-in figure,
#: so a tier can be retuned without a deploy.
CLUE_TIERS = {
    "clue scroll (beginner)": {"tier": "beginner", "rate_kph": 19.0},
    "clue scroll (easy)": {"tier": "easy", "rate_kph": 20.5},
    "clue scroll (medium)": {"tier": "medium", "rate_kph": 11.2},
    "clue scroll (hard)": {"tier": "hard", "rate_kph": 7.8},
    "clue scroll (elite)": {"tier": "elite", "rate_kph": 6.3},
    "clue scroll (master)": {"tier": "master", "rate_kph": 4.7},
}

#: ``{receipt item name: clue pseudo-NPC}`` — the reverse of
#: :data:`CLUE_TIERS` x :data:`CLUE_SOURCE_PREFIXES`, built once because it is
#: consulted per drop envelope. Names that no item actually uses ("clue bottle
#: (master)" — masters have no skilling source) simply never match.
_CLUE_SOURCE_NPCS = {
    f"{prefix} ({entry['tier']})": npc
    for npc, entry in CLUE_TIERS.items()
    for prefix in CLUE_SOURCE_PREFIXES
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


def roll_scope(npc_norm: str) -> str:
    """Watermark scope for a clue tier's ROLL counter.

    Rolls arrive at other NPCs' kill counts (a Hellhound's scroll box), so this
    scope's dedupe keys are the *source* kill's, never the casket's — which is
    the other reason it cannot share ``eff:``.
    """
    return f"{ROLL_SCOPE_PREFIX}:{_norm(npc_norm).replace(' ', '_')}"


def clue_tier(npc_norm) -> Optional[dict]:
    """The :data:`CLUE_TIERS` entry for a clue pseudo-NPC, or ``None``."""
    return CLUE_TIERS.get(_norm(npc_norm))


def clue_roll_npc(item_name) -> Optional[str]:
    """The clue pseudo-NPC an item receipt is a roll for, or ``None``.

    ``clue_roll_npc("Scroll box (hard)") == "clue scroll (hard)"``: the box and
    the scroll are the same event as far as the event window is concerned, so
    both credit the tier the casket will later be opened under.
    """
    return _CLUE_SOURCE_NPCS.get(_norm(item_name))


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


def _price_clue_row(row, kills: int, derived_rates: Optional[dict]) -> Optional[tuple]:
    """``(hours, paired, rolled)`` for a :data:`CLUE_TIERS` NPC, or ``None``
    when this row is not one.

    ``kills`` are caskets opened inside the event window; ``rolls`` are scrolls
    the player was dealt inside it. Only ``min`` of the two is paid for, so a
    banked stack dumped on day one is worth whatever the player actually rolled
    while the event was running and not one clue more. The openings still show:
    the row keeps its kill count, it just doesn't price.

    Rate order mirrors the rest of the module — an ``npc_ehb_rates`` row for the
    pseudo-NPC wins, so a tier can be retuned in the DB; otherwise the measured
    default from :data:`CLUE_TIERS`. Either way the hours are OUR estimate (WOM
    prices no clue metric), so the caller flags them ``estimated`` outright
    rather than only when the fallback was used.
    """
    entry = clue_tier(row.get("npc_name"))
    if entry is None:
        return None
    try:
        rolled = max(0, int(row.get("rolls") or 0))
    except (TypeError, ValueError):
        rolled = 0
    paired = min(max(kills, 0), rolled)
    rate = _derived_rate(derived_rates, row.get("npc_id")) or float(entry["rate_kph"])
    hours = (paired / rate) if (rate > 0 and paired) else 0.0
    return hours, paired, rolled


def rows_to_summary(rows: Iterable[dict], rates: dict,
                    derived_rates: Optional[dict] = None) -> dict:
    """Fold one player's effort rows into the shape the read model exposes.

    ``rows`` are ``{"npc_id", "npc_name", "boss_metric", "kills", "completions",
    "rolls", "last_at", "frozen_at"}``. Returns ``{"ehb_hours",
    "ehb_estimated_hours", "kills",
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
    not asking. :data:`CLUE_TIERS` NPCs take a third (:func:`_price_clue_row`),
    pricing only the openings a matching in-window roll paid for.
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
        clue_priced = _price_clue_row(row, kills, derived_rates)
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
        rolled = paired = None
        if marker_priced is not None:
            hours, row_estimated_hours = marker_priced
            estimated = row_estimated_hours > 0
        elif clue_priced is not None:
            hours, paired, rolled = clue_priced
            # Always our own number, never WOM's — see _price_clue_row.
            estimated = hours > 0
            row_estimated_hours = hours
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
                # Clue tiers only (None elsewhere): scrolls dealt in-window and
                # the subset of openings those paid for. Surfaced so a player
                # looking at "60 opened, 3h" can see it was 20 that counted.
                "rolled": rolled,
                "paired": paired,
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
