"""Competition scoring (the ``sotw`` / ``botw`` event kinds).

A competition is a time-boxed race of INDIVIDUALS: most XP gained in one
skill (sotw) or most KC gained at one boss / NPC group (botw) between the
event's start and end — the classic WiseOldMan "Skill/Boss of the Week",
except the duration is whatever the admins choose and DropTracker can layer
**bonus points** on top from plugin data WOM never sees:

- ``pet``        — a NEW pet from the event boss (botw) or the skill's
                   skilling pet (sotw): +N points, at most ``max_awards``
                   per player (a new pet is inherently once, but the cap is
                   still enforced in the fold for defense in depth).
- ``time_under`` — a kill of the rule's NPC in ``threshold_ms`` or better:
                   +N points, at most ``max_awards`` per player. Multiple
                   tiers may coexist ("under 1:00 = 5, under 0:50 = 15
                   more") — each rule matches independently, so one fast
                   kill can award several rules at once.
- ``task``       — ANY criteria the event task builder can express, embedded
                   verbatim as ``{type, target, target_value, config}`` and
                   evaluated by the engine's own ``match_task`` against a
                   synthetic task dict. "Collect all four Zulrah uniques",
                   "500 points of Vorkath loot", "10 Zulrah collection-log
                   slots", "a sub-2:00 kill OR the pet" — the whole
                   vocabulary, for one new rule type. The write validator
                   INJECTS the raced NPCs into the embedded config's source
                   restriction, so scoping is enforced at match time by the
                   same predicate real tasks use, not by admin convention.
                   Pays ``points`` per ``need`` units of progress, up to
                   ``max_awards`` times.
- ``milestone``  — every ``step`` units of GAINED metric pays ``points``
                   ("every 100 kills = 10 pts"), up to ``max_awards`` times.
                   Folds straight off the gained number and writes NO ledger
                   rows at all, so it cannot double-count and needs no matcher.

Everything lands on ONE hidden ``competition`` task per event, so per-player
standings fold from a single ledger. Rows tagged ``bonus:{type}:{rule_id}``
(:func:`bonus_note`) belong to a bonus rule; every other row's ``quantity`` is
gained metric units (XP delta / kills). What a tagged row's ``quantity`` MEANS
depends on the rule type, and the note's type segment is what tells them apart
without loading the config:

- ``pet`` / ``time_under`` — one row IS one award; ``quantity`` is the points.
- ``task`` — one row is one unit of PROGRESS (a drop, a kill, GP, a clog slot);
  ``quantity`` is credit units and the points come from the rule at fold time.

The task never "completes".

Ranking is per event (``ranking.mode``):

- ``gained`` (default) — the leaderboard is raw gained, exactly what WOM
  would show; bonus points ride along as a secondary column/mini-board.
- ``points`` — gained converts at ``floor(gained / gained_per_point)`` and
  bonus points stack on top: one combined number.

Scoring is a **pure function of the applied ledger** (recomputed from every
surviving row), so apply/revoke are simple deltas, per-player caps self-heal
when an award is revoked, and nothing here can drift. No I/O — the module
stays unit-testable (the pytest conftest stubs ``db``/``services``).

Wiring: matcher/apply/revoke in :mod:`services.event_engine`; write-time
config validation in :mod:`web_api.routes.event_task_validation`; the event
scaffold (hidden task + roster team) in :mod:`services.competition_setup`;
WOM linkage/polling in :mod:`services.competition_wom`.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

# The one import this module makes. utils.task_progress is a stdlib-only leaf
# holding the pure "how far along is this?" folds that services.event_engine
# also uses — it lives under utils/ precisely so this module can reach it (the
# pytest conftest MagicMocks the whole ``services`` package, so a service that
# imported a sibling service would silently score against a mock).
from utils.task_progress import PROGRESS_KINDS, progress_from_rows

# The event kinds this module implements (db.models.COMPETITION_EVENT_KINDS
# mirrors this — kept literal here so the module stays import-free/pure).
COMPETITION_KINDS = ("sotw", "botw")
COMPETITION_TASK_TYPE = "competition"

METRIC_KINDS = ("skill", "boss")
RANKING_MODES = ("gained", "points")
BONUS_RULE_TYPES = ("pet", "time_under", "task", "milestone")

# Rule types whose ledger rows are AWARDS (one row = one payout, quantity =
# points) rather than units of progress. The note's type segment is the only
# thing a reader needs to tell the two ledger dialects apart.
DISCRETE_RULE_TYPES = ("pet", "time_under")

# Task types the ``task`` rule may embed. ``kc_target``/``xp_target`` are
# deliberately absent: the race ALREADY scores those kills and that XP, and
# their matcher modes feed the shared KC-watermark / XP-baseline folds, which
# would corrupt ``gained`` itself. A "every N kills" bonus is a ``milestone``.
BONUS_TASK_TYPES = ("item_collection", "loot_value", "pb_target",
                    "pet_collection", "skill_target", "ca_target")

# Progress kinds that can pay MORE THAN ONCE. The others fold a set (distinct
# items, grouped requirements, a percent-scaled either-or) that saturates by
# construction, so a second award could never be earned — pinning max_awards
# to 1 there keeps "Award 1 of 3" from being a lie in the UI.
REPEATABLE_PROGRESS_KINDS = ("count", "points")

# Embedded task types whose match condition is a PERSISTENT STATE rather than
# an event: ``skill_target`` matches on every experience envelope once the
# level is reached, so with ``max_awards`` above 1 the next XP drop would pay
# again, and again, until the cap. Pinned in the scorer (not only the
# validator) so an already-stored config is corrected on read too.
SINGLE_AWARD_TASK_TYPES = ("skill_target",)

DEFAULT_RANKING_MODE = "gained"
# Points-mode conversion defaults, per metric kind: 10k XP or 1 kill = 1 pt.
DEFAULT_GAINED_PER_POINT_SKILL = 10_000
DEFAULT_GAINED_PER_POINT_BOSS = 1

# Bounds the write validator enforces (kept here so scoring and validation
# agree on what a legal config looks like).
MAX_NPCS = 10                       # matches kc_target's multi-NPC cap
# Raised from 6 (one pet rule + five time tiers) now that a rule can be any
# task. NOT raised further: ``record_match`` builds ``f"{guid[:60]}#b{id}"``
# into a String(64) column, so a three-digit rule id shears and ``#b100``
# would collide with ``#b10``.
MAX_BONUS_RULES = 12
MAX_RULE_NEED = 1_000_000_000       # progress units one award may demand
MIN_MILESTONE_STEP = 1
MAX_MILESTONE_STEP = 1_000_000_000
MIN_BONUS_POINTS = 1
MAX_BONUS_POINTS = 10_000_000
MIN_AWARDS_PER_PLAYER = 1
MAX_AWARDS_PER_PLAYER = 100
MIN_GAINED_PER_POINT = 1
MAX_GAINED_PER_POINT = 1_000_000_000
MIN_TIME_THRESHOLD_MS = 600         # one game tick
MAX_TIME_THRESHOLD_MS = 6 * 60 * 60 * 1000  # sanity: no 6h+ "fast" kills

# Ledger-note tag binding a bonus award to its rule: ``bonus:{type}:{id}``.
# Same shape family as the engine's ``path:N`` tag — an admin note may ride
# after `` | `` and both parsers tolerate it.
BONUS_NOTE_PREFIX = "bonus:"

# Ledger statuses that count toward standings — must stay in lockstep with
# the engine's APPLIED_BONUS_STATUSES / rollup status set.
APPLIED_STATUSES = ("auto", "confirmed", "manual")


def _norm(value) -> str:
    """Case-insensitive name key — identical to ``event_engine._norm`` so a
    ledger row's ``matched_target`` / an envelope's names line up with config
    NPC / pet / skill names."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, default: int, lo: int, hi: int) -> int:
    n = _int(value, default)
    if value is None:
        n = default
    return min(max(n, lo), hi)


# --------------------------------------------------------------------------- #
# Bonus-note tags
# --------------------------------------------------------------------------- #
def bonus_note(rule_type: str, rule_id: int) -> str:
    """The engine-written ledger tag for one bonus award."""
    return f"{BONUS_NOTE_PREFIX}{rule_type}:{int(rule_id)}"


def parse_bonus_note(note) -> Optional[tuple]:
    """``(rule_type, rule_id)`` for a bonus-tagged ledger note, else None.
    Tolerates an admin note after `` | `` (manual awards).

    The type segment is matched on SHAPE, not against
    :data:`BONUS_RULE_TYPES` — a row written by a newer deploy must still be
    recognised as a bonus row by an older one. Failing to recognise it is the
    dangerous direction: every consumer downstream (:func:`fold_rows`, the
    record-time gate, the award log) treats an unparsed row as GAINED, so an
    unknown rule type would silently add its points to the ranked metric.
    Recognising it and finding no matching rule pays nothing, which is wrong
    but bounded. The rule id is the load-bearing half."""
    if not note:
        return None
    tag = str(note).split("|", 1)[0].strip()
    if not tag.startswith(BONUS_NOTE_PREFIX):
        return None
    parts = tag[len(BONUS_NOTE_PREFIX):].split(":")
    if len(parts) < 2:
        return None
    rule_type = parts[0]
    if not rule_type or len(rule_type) > 32 or not rule_type.replace("_", "").isalnum():
        return None
    try:
        return rule_type, int(parts[1])
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class CompetitionBonusRule:
    """One normalized bonus rule (see the module docstring for semantics)."""

    __slots__ = ("id", "type", "points", "max_awards", "pets", "npc",
                 "threshold_ms", "label", "task", "progress_kind", "need",
                 "kinds", "scope", "step", "unscoped", "metric_kind")

    def __init__(self, raw: dict, idx: int, metric_kind=None):
        raw = raw if isinstance(raw, dict) else {}
        self.type = raw.get("type") if raw.get("type") in BONUS_RULE_TYPES else None
        # Server-assigned, stable across edits; 1-based position fallback for
        # hand-built configs so ledger tags always have a real id.
        self.id = _int(raw.get("id"), idx + 1) or (idx + 1)
        self.points = _clamp(raw.get("points"), MIN_BONUS_POINTS,
                             MIN_BONUS_POINTS, MAX_BONUS_POINTS)
        self.max_awards = _clamp(raw.get("max_awards"), 1,
                                 MIN_AWARDS_PER_PLAYER, MAX_AWARDS_PER_PLAYER)
        # pet: the explicit allow-list (resolved at authoring — the validator
        # stores real pet names; no runtime taxonomy lookups here).
        self.pets = tuple(n for n in (_norm(p) for p in (raw.get("pets") or ()))
                          if n)
        # time_under: one NPC + a tick-precision threshold.
        self.npc = _norm(raw.get("npc")) or None
        self.threshold_ms = _clamp(raw.get("threshold_ms"), 0,
                                   0, MAX_TIME_THRESHOLD_MS)
        self.label = (str(raw.get("label")).strip()[:120]
                      if raw.get("label") else None)

        # task: the embedded task-builder criteria, plus the shape the
        # validator DERIVED from them once at write time. Deriving them here
        # instead would mean this module had to know the task-type vocabulary,
        # and a config whose shape drifted could silently change how history
        # was counted.
        task = raw.get("task")
        self.task = task if isinstance(task, dict) else None
        self.progress_kind = (raw.get("progress_kind")
                              if raw.get("progress_kind") in PROGRESS_KINDS
                              else "count")
        self.need = _clamp(raw.get("need"), 1, 1, MAX_RULE_NEED)
        # Envelope kinds that can possibly credit this rule — a cheap
        # pre-filter so the hot path skips match_task for the other 90%.
        # Empty means "no filter" (correct, just slower).
        self.kinds = tuple(str(k) for k in (raw.get("kinds") or ()) if k)
        # Display only: the NPCs the criteria were scoped to. Enforcement
        # lives in the embedded config's source restriction, not here.
        self.scope = tuple(_norm(n) for n in (raw.get("scope") or ()) if _norm(n))
        # sotw item rules cannot be skill-scoped (there is no item->skill
        # dataset), so they count from anywhere and say so on every surface.
        self.unscoped = bool(raw.get("unscoped"))

        # milestone: gained units per payout. ``metric_kind`` rides along from
        # the config purely so the label can name the unit ("kills" vs "XP")
        # instead of guessing it from the step's magnitude.
        self.step = _clamp(raw.get("step"), MIN_MILESTONE_STEP,
                           MIN_MILESTONE_STEP, MAX_MILESTONE_STEP)
        self.metric_kind = metric_kind

        if self.type == "task" and (
                self.progress_kind not in REPEATABLE_PROGRESS_KINDS
                or (self.task or {}).get("type") in SINGLE_AWARD_TASK_TYPES):
            # A set/percent fold saturates, so a second award is unreachable —
            # and a state-condition type would pay on EVERY later envelope.
            self.max_awards = 1

    @property
    def valid(self) -> bool:
        if self.type == "pet":
            return bool(self.pets)
        if self.type == "time_under":
            return bool(self.npc) and self.threshold_ms >= MIN_TIME_THRESHOLD_MS
        if self.type == "task":
            return bool(self.task) and bool(self.task.get("type")) and self.need >= 1
        if self.type == "milestone":
            return self.step >= MIN_MILESTONE_STEP
        return False

    @property
    def cap_units(self) -> int:
        """Progress units past which nothing more can ever be earned — the
        ceiling every fold and record-time gate clamps to."""
        return self.need * max(self.max_awards, 1)


class CompetitionConfig:
    """Normalized competition task config (accepts a dict or a raw JSON
    string — task configs reach some callers unparsed)."""

    __slots__ = ("metric_kind", "skill", "npcs", "ranking_mode",
                 "gained_per_point", "bonus_rules", "rules_by_id")

    def __init__(self, raw):
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        raw = raw if isinstance(raw, dict) else {}
        self.metric_kind = (raw.get("metric_kind")
                            if raw.get("metric_kind") in METRIC_KINDS else None)
        self.skill = _norm(raw.get("skill")) or None
        npcs: list = []
        for name in (raw.get("npcs") or ())[:MAX_NPCS]:
            norm = _norm(name)
            if norm and norm not in npcs:
                npcs.append(norm)
        self.npcs = tuple(npcs)

        ranking = raw.get("ranking") if isinstance(raw.get("ranking"), dict) else {}
        self.ranking_mode = (ranking.get("mode")
                             if ranking.get("mode") in RANKING_MODES
                             else DEFAULT_RANKING_MODE)
        default_rate = (DEFAULT_GAINED_PER_POINT_BOSS
                        if self.metric_kind == "boss"
                        else DEFAULT_GAINED_PER_POINT_SKILL)
        self.gained_per_point = _clamp(ranking.get("gained_per_point"),
                                       default_rate,
                                       MIN_GAINED_PER_POINT, MAX_GAINED_PER_POINT)

        rules = []
        for idx, raw_rule in enumerate((raw.get("bonus_rules") or ())[:MAX_BONUS_RULES]):
            rule = CompetitionBonusRule(raw_rule, idx, self.metric_kind)
            if rule.valid:
                rules.append(rule)
        self.bonus_rules = tuple(rules)
        self.rules_by_id = {r.id: r for r in self.bonus_rules}

    @property
    def valid(self) -> bool:
        """A scoreable config: a skill race with a skill, or a boss race with
        at least one NPC."""
        if self.metric_kind == "skill":
            return bool(self.skill)
        if self.metric_kind == "boss":
            return bool(self.npcs)
        return False

    # ---- matcher precompute -------------------------------------------------
    def matcher_index(self) -> dict:
        """Plain-data snapshot for the state-load task dict — the engine's
        matcher works off this without importing the module (mirrors
        ``loot_sweep_index``)."""
        return {
            "metric_kind": self.metric_kind,
            "skill": self.skill,
            "npcs": list(self.npcs),
            "pet_rules": {
                pet: {"id": r.id, "points": r.points}
                for r in self.bonus_rules if r.type == "pet"
                for pet in r.pets
            },
            "time_rules": [
                {"id": r.id, "npc": r.npc, "threshold_ms": r.threshold_ms,
                 "points": r.points}
                for r in self.bonus_rules if r.type == "time_under"
            ],
            # Embedded task criteria, verbatim. The engine turns each ``task``
            # blob into a synthetic task dict at state load and runs the SAME
            # ``match_task`` over it that a real task gets — that is the whole
            # trick, and why one rule type buys the entire builder vocabulary.
            "task_rules": [
                {"id": r.id, "kinds": list(r.kinds), "task": dict(r.task or {})}
                for r in self.bonus_rules if r.type == "task"
            ],
        }

    @property
    def has_milestones(self) -> bool:
        return any(r.type == "milestone" for r in self.bonus_rules)


# --------------------------------------------------------------------------- #
# Ledger folds
# --------------------------------------------------------------------------- #
def _row_sort_key(row):
    """Deterministic award order — "the first ``max_awards`` qualifying kills
    count" must not depend on query order. created_at may be None on unsaved
    rows folded via ``include``; those sort last (they are the newest)."""
    created = getattr(row, "created_at", None)
    return (created is None, created, getattr(row, "id", None) or 0)


def fold_rows(rows: Iterable, config: CompetitionConfig) -> dict:
    """Per-player standings input from applied ledger rows.

    Returns ``{player_id: {"gained": int, "bonus_points": int,
    "bonus": {rule_id: {"type", "count", "awarded", "points",
    "progress", "need"}}}}``.

    Three dialects share the ledger, told apart by the note's type segment
    (see the module docstring) and folded in three passes:

    1. **Untagged** rows are gained metric units. **Discrete** rules
       (``pet`` / ``time_under``) count one award per row, ``quantity`` being
       the points; ``awarded`` / ``points`` stop at the rule's per-player cap
       (rows beyond it contribute 0). **Task** rows are only bucketed here.
    2. **Task** rules fold their bucketed rows through the shared progress
       cores — the same functions the real task would use — then pay
       ``points`` per ``need`` units, capped at ``max_awards``. The points
       come from the RULE, not the row, so a partly-collected set pays
       nothing and a 3× stack of one listed item is worth one item.
    3. **Milestone** rules read the gained total computed in pass 1 and write
       no ledger rows at all.

    The cap is enforced HERE, not only at record time, so a revoked award
    frees its slot and an over-cap row that slipped a gate can never pay out.
    """
    per: dict = {}
    task_rows: dict = {}
    for row in sorted(rows, key=_row_sort_key):
        player_id = getattr(row, "player_id", None)
        if player_id is None:
            continue
        entry = per.setdefault(player_id,
                               {"gained": 0, "bonus_points": 0, "bonus": {}})
        quantity = max(_int(getattr(row, "quantity", 1), 1), 1)
        parsed = parse_bonus_note(getattr(row, "note", None))
        if parsed is None:
            entry["gained"] += quantity
            continue
        rule_type, rule_id = parsed
        rule = config.rules_by_id.get(rule_id)
        # Trust the CONFIG's type over the note's when the rule still exists:
        # a draft-time edit may have changed the dialect under rows already
        # written, and the fold must read them the way the live rule means.
        effective_type = rule.type if rule is not None else rule_type
        slot = entry["bonus"].setdefault(
            rule_id, {"type": effective_type, "count": 0,
                      "awarded": 0, "points": 0})
        slot["count"] += 1
        if effective_type == "task":
            task_rows.setdefault((player_id, rule_id), []).append(row)
            continue
        if effective_type not in DISCRETE_RULE_TYPES:
            # ``milestone`` (writes no rows), or a dialect this deploy has
            # never heard of. Recognised as a bonus row — which is what keeps
            # it out of ``gained`` — but it pays nothing, because reading a
            # row's quantity as points is only safe for the discrete types.
            continue
        cap = rule.max_awards if rule is not None else 1
        if slot["awarded"] >= cap:
            continue  # over the per-player cap — dead weight, pays nothing
        # A discrete row's quantity IS the points at award time; a rule edit
        # while drafting doesn't rewrite history (configs lock at activation).
        slot["awarded"] += 1
        slot["points"] += quantity
        entry["bonus_points"] += quantity

    for (player_id, rule_id), rule_rows in task_rows.items():
        rule = config.rules_by_id.get(rule_id)
        slot = per[player_id]["bonus"][rule_id]
        if rule is None:
            continue  # rule deleted while drafting — its rows pay nothing
        progress = progress_from_rows(
            rule_rows, rule.progress_kind,
            (rule.task or {}).get("config") or {}, rule.cap_units)
        awarded = min(progress // rule.need, rule.max_awards)
        slot["progress"] = progress
        slot["need"] = rule.need
        slot["awarded"] = awarded
        slot["points"] = awarded * rule.points
        per[player_id]["bonus_points"] += slot["points"]

    for rule in config.bonus_rules:
        if rule.type != "milestone":
            continue
        for entry in per.values():
            awarded = min(_int(entry.get("gained")) // rule.step, rule.max_awards)
            if awarded <= 0:
                continue
            points = awarded * rule.points
            entry["bonus"][rule.id] = {
                "type": "milestone", "count": awarded, "awarded": awarded,
                "points": points, "progress": _int(entry.get("gained")),
                "need": rule.step}
            entry["bonus_points"] += points
    return per


def bonus_award_count(rows: Iterable, player_id, rule_id: int) -> int:
    """Surviving award rows one player holds for one rule — the record-time
    cap gate's input (``_row_advances_progress``). Counts every applied row,
    capped or not, because a row past the cap should not even be recorded."""
    n = 0
    for row in rows:
        if getattr(row, "player_id", None) != player_id:
            continue
        parsed = parse_bonus_note(getattr(row, "note", None))
        if parsed is not None and parsed[1] == int(rule_id):
            n += 1
    return n


def rule_rows(rows: Iterable, player_id, rule_id: int) -> list:
    """One player's surviving ledger rows for one rule — the input both the
    task fold and the record-time gate work from."""
    out = []
    for row in rows:
        if getattr(row, "player_id", None) != player_id:
            continue
        parsed = parse_bonus_note(getattr(row, "note", None))
        if parsed is not None and parsed[1] == int(rule_id):
            out.append(row)
    return out


def task_rule_progress(rows: Iterable, rule: CompetitionBonusRule) -> int:
    """Progress units the given rows represent toward one ``task`` rule,
    clamped at the point past which nothing more can be earned.

    The record-time gate (``_row_advances_progress``) calls this twice — with
    and without the candidate row — and records only if the number moved. That
    is deliberately PROGRESS, not awards: the third item of a five-item set
    earns nothing yet but must still be recorded, or the set can never
    complete."""
    return progress_from_rows(rows, rule.progress_kind,
                              (rule.task or {}).get("config") or {},
                              rule.cap_units)


def points_for_gained(gained: int, config: CompetitionConfig) -> int:
    """Points-mode conversion of gained metric units (floor)."""
    rate = max(config.gained_per_point, 1)
    return max(_int(gained), 0) // rate


def player_points(entry: dict, config: CompetitionConfig) -> int:
    """One player's combined points (points mode): converted gained + bonus."""
    return points_for_gained(entry.get("gained", 0), config) + _int(entry.get("bonus_points"))


def rank_value(entry: dict, config: CompetitionConfig) -> int:
    """The number a player is RANKED by under the event's ranking mode."""
    if config.ranking_mode == "points":
        return player_points(entry, config)
    return _int(entry.get("gained"))


def team_totals(per_player: dict, config: CompetitionConfig) -> tuple:
    """``(gained_total, score_total)`` across every player — the (single)
    roster team's ``EventProgress.progress`` and ``EventTeam.score``."""
    gained_total = sum(_int(e.get("gained")) for e in per_player.values())
    score_total = sum(rank_value(e, config) for e in per_player.values())
    return gained_total, score_total


# --------------------------------------------------------------------------- #
# Standings
# --------------------------------------------------------------------------- #
def standings(per_player: dict, config: CompetitionConfig, names: dict,
              wom_rows: Optional[list] = None) -> list:
    """Merged, ranked standings rows.

    ``per_player`` is :func:`fold_rows` output (DT-tracked players);
    ``names`` maps player_id -> display name. ``wom_rows`` (linked/created
    events) is the cached WOM participation list — dicts with
    ``wom_player_id`` / ``display_name`` / ``gained`` and optionally the
    resolved ``player_id``. WOM rows whose player is already in
    ``per_player`` are dropped (the ledger row is richer — it carries plugin
    top-ups and bonuses); the rest render as unregistered display-only rows
    (no bonus points — bonuses need plugin data, which needs an account).
    """
    rows: list = []
    seen_norm_names = set()
    for player_id, entry in per_player.items():
        name = names.get(player_id) or f"Player {player_id}"
        seen_norm_names.add(_norm(name))
        rows.append({
            "player_id": player_id,
            "wom_player_id": None,
            "player_name": name,
            "registered": True,
            "gained": _int(entry.get("gained")),
            "bonus_points": _int(entry.get("bonus_points")),
            "points": player_points(entry, config),
            "bonus": entry.get("bonus") or {},
        })
    known_ids = set(per_player)
    for raw in wom_rows or ():
        if not isinstance(raw, dict):
            continue
        resolved = raw.get("player_id")
        if resolved is not None and resolved in known_ids:
            continue
        name = str(raw.get("display_name") or raw.get("player_name") or "").strip()
        if name and _norm(name) in seen_norm_names:
            # Same account under a not-yet-healed wom_id mismatch — the DT row
            # already shows it; a duplicate greyed row would double-display.
            continue
        gained = max(_int(raw.get("gained")), 0)
        entry = {"gained": gained, "bonus_points": 0}
        rows.append({
            "player_id": resolved,
            "wom_player_id": raw.get("wom_player_id"),
            "player_name": name or "Unknown",
            "registered": False,
            "gained": gained,
            "bonus_points": 0,
            "points": player_points(entry, config),
            "bonus": {},
        })
    key = ("points" if config.ranking_mode == "points" else "gained")
    rows.sort(key=lambda r: (-r[key], -r["bonus_points"], -r["gained"],
                             _norm(r["player_name"])))
    for i, row in enumerate(rows):
        row["rank"] = i + 1
    return rows


# --------------------------------------------------------------------------- #
# Display helpers (shared by web + Discord so the wording never diverges)
# --------------------------------------------------------------------------- #
def format_gained(value: int, metric_kind: Optional[str]) -> str:
    """``2_481_034`` -> ``2.48M XP`` / ``312`` -> ``312 KC`` — the OSRS-style
    abbreviation every leaderboard surface shows for gained values."""
    value = max(_int(value), 0)
    if value >= 1_000_000_000:
        num = f"{value / 1_000_000_000:.2f}".rstrip("0").rstrip(".") + "B"
    elif value >= 1_000_000:
        num = f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    elif value >= 100_000:
        num = f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    else:
        num = f"{value:,}"
    unit = "XP" if metric_kind == "skill" else "KC"
    return f"{num} {unit}"


def score_text(entry_value: int, config: CompetitionConfig) -> str:
    """The ranked number, worded for its ranking mode — ``"2.48M XP"`` /
    ``"312 KC"`` in gained mode, ``"270 pts"`` in points mode."""
    if config.ranking_mode == "points":
        return f"{max(_int(entry_value), 0):,} pts"
    return format_gained(entry_value, config.metric_kind)


def format_time_ms(ms: int) -> str:
    """``91_800`` -> ``1:31.8`` — OSRS kill-time style (tick precision keeps
    at most one decimal place; whole seconds drop it)."""
    ms = max(_int(ms), 0)
    total_seconds, rem_ms = divmod(ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    if rem_ms:
        return f"{minutes}:{seconds:02d}.{rem_ms // 100}"
    return f"{minutes}:{seconds:02d}"


def metric_line(config: CompetitionConfig) -> Optional[str]:
    """One-line race description for announcements — ``**Skill** Mining —
    most XP gained wins`` / ``**Boss** Zulrah — most kills gained wins``."""
    if config.metric_kind == "skill" and config.skill:
        return f"**Skill** {config.skill.title()} — most XP gained wins"
    if config.metric_kind == "boss" and config.npcs:
        names = ", ".join(n.title() for n in config.npcs[:3])
        if len(config.npcs) > 3:
            names += f" (+{len(config.npcs) - 3} more)"
        return f"**Boss** {names} — most kills gained wins"
    return None


def rule_label(rule: CompetitionBonusRule) -> str:
    """Human sentence for one rule — the wizard preview, the "how points
    work" card and the Discord award line all render exactly this."""
    if rule.label:
        return rule.label
    if rule.type == "pet":
        if len(rule.pets) == 1:
            return f"New pet: {rule.pets[0].title()}"
        return "New pet"
    if rule.type == "time_under":
        npc = (rule.npc or "").title()
        return f"{npc} kill under {format_time_ms(rule.threshold_ms)}"
    if rule.type == "milestone":
        # The unit is a property of the RACE, not of the step's size — a boss
        # race with a 1,000-kill milestone was reading "Every 1,000 XP".
        unit = "XP" if rule.metric_kind == "skill" else "kills"
        return f"Every {rule.step:,} {unit}"
    if rule.type == "task":
        return task_rule_label(rule)
    return "Bonus"


def task_rule_label(rule: CompetitionBonusRule) -> str:
    """Fallback sentence for a ``task`` rule the admin didn't name. The
    validator normally writes a ``label``, so this is the safety net — it
    describes the SHAPE, since the criteria themselves can be a 40-item list."""
    task = rule.task or {}
    ttype = task.get("type")
    need = rule.need
    if ttype == "loot_value":
        return f"{format_gained(need, None).replace(' KC', '')} GP of loot"
    if ttype == "pb_target":
        return "Fast kill" if need <= 1 else f"{need:,} fast kills"
    if ttype == "ca_target":
        return "A combat achievement" if need <= 1 else f"{need:,} combat achievements"
    if ttype == "pet_collection":
        return "A pet"
    if ttype == "skill_target":
        return f"Reach level {need}"
    if rule.progress_kind == "distinct":
        return f"Collect all {need:,} listed drops"
    if rule.progress_kind == "groups":
        return "Complete the listed sets"
    if rule.progress_kind == "any_path":
        return "Complete any one of the listed goals"
    if rule.progress_kind == "points":
        return f"{need:,} points of listed loot"
    return "A listed drop" if need <= 1 else f"{need:,} listed drops"


def rule_scope_line(rule: CompetitionBonusRule) -> Optional[str]:
    """Where a rule's criteria count — the honesty line every surface shows.

    ``None`` for rules that are inherently scoped (a kill time names its
    boss). A sotw item rule returns the "counts anywhere" wording: there is no
    item-to-skill dataset, so it genuinely cannot be scoped to the raced
    skill, and participants must not be left to assume otherwise."""
    if rule.unscoped:
        return "counts anywhere"
    if rule.scope:
        names = ", ".join(n.title() for n in rule.scope[:3])
        if len(rule.scope) > 3:
            names += f" (+{len(rule.scope) - 3} more)"
        return f"at {names}"
    return None


def bonus_detail(rule_id: int, config: CompetitionConfig,
                 awarded_n: int, matched_target=None,
                 time_text: Optional[str] = None,
                 points: Optional[int] = None) -> dict:
    """Notification payload for one bonus award (``event_competition_bonus``):
    everything the message layout needs, pre-worded. ``time_text`` is the
    kill time recovered from the ledger note's human half (``bonus:… | 0:52.6``)
    — the row is the only durable carrier of it. ``points`` overrides the
    rule's per-award value with what this award actually moved the total by
    (task rules pay ``points`` per completed ``need``, which is not the same as
    one row's quantity)."""
    rule = config.rules_by_id.get(int(rule_id))
    if rule is None:
        return {"rule_id": int(rule_id), "label": "Bonus",
                "points": _int(points), "cap_line": None, "type": None,
                "reason": "Bonus", "scope_line": None}
    if rule.type == "time_under" and time_text:
        reason = (f"{(rule.npc or '').title()} in {time_text} "
                  f"(under {format_time_ms(rule.threshold_ms)})")
    elif rule.type == "pet" and matched_target:
        reason = f"New pet: {str(matched_target).strip()}"
    elif rule.type == "task" and matched_target and rule.need <= 1:
        # A one-shot task rule is fully described by what triggered it.
        reason = f"{rule_label(rule)}: {str(matched_target).strip()}"
    else:
        reason = rule_label(rule)
    cap_line = (f"Award {min(awarded_n, rule.max_awards)} of {rule.max_awards}"
                if rule.max_awards > 1 else None)
    return {
        "rule_id": rule.id,
        "type": rule.type,
        "points": _int(points) if points is not None else rule.points,
        "label": rule_label(rule),
        "reason": reason,
        "scope_line": rule_scope_line(rule),
        "cap_line": cap_line,
        "max_awards": rule.max_awards,
        "awarded_n": min(awarded_n, rule.max_awards),
    }
