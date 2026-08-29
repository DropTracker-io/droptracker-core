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

Everything lands on ONE hidden ``competition`` task per event, so per-player
standings fold from a single ledger: rows tagged ``bonus:{type}:{rule_id}``
(:func:`bonus_note`) are bonus awards whose ``quantity`` is the points; every
other row's ``quantity`` is gained metric units (XP delta / kills). The task
never "completes".

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

# The event kinds this module implements (db.models.COMPETITION_EVENT_KINDS
# mirrors this — kept literal here so the module stays import-free/pure).
COMPETITION_KINDS = ("sotw", "botw")
COMPETITION_TASK_TYPE = "competition"

METRIC_KINDS = ("skill", "boss")
RANKING_MODES = ("gained", "points")
BONUS_RULE_TYPES = ("pet", "time_under")

DEFAULT_RANKING_MODE = "gained"
# Points-mode conversion defaults, per metric kind: 10k XP or 1 kill = 1 pt.
DEFAULT_GAINED_PER_POINT_SKILL = 10_000
DEFAULT_GAINED_PER_POINT_BOSS = 1

# Bounds the write validator enforces (kept here so scoring and validation
# agree on what a legal config looks like).
MAX_NPCS = 10                       # matches kc_target's multi-NPC cap
MAX_BONUS_RULES = 6                 # one pet rule + up to 5 time tiers
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
    Tolerates an admin note after `` | `` (manual awards)."""
    if not note:
        return None
    tag = str(note).split("|", 1)[0].strip()
    if not tag.startswith(BONUS_NOTE_PREFIX):
        return None
    parts = tag[len(BONUS_NOTE_PREFIX):].split(":")
    if len(parts) != 2 or parts[0] not in BONUS_RULE_TYPES:
        return None
    try:
        return parts[0], int(parts[1])
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class CompetitionBonusRule:
    """One normalized bonus rule (see the module docstring for semantics)."""

    __slots__ = ("id", "type", "points", "max_awards", "pets", "npc",
                 "threshold_ms", "label")

    def __init__(self, raw: dict, idx: int):
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

    @property
    def valid(self) -> bool:
        if self.type == "pet":
            return bool(self.pets)
        if self.type == "time_under":
            return bool(self.npc) and self.threshold_ms >= MIN_TIME_THRESHOLD_MS
        return False


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
            rule = CompetitionBonusRule(raw_rule, idx)
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
        }


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
    "bonus": {rule_id: {"type", "count", "awarded", "points"}}}}`` where
    ``count`` is every surviving award row and ``awarded`` / ``points`` stop
    at the rule's per-player cap (rows beyond it contribute 0 — the cap is
    enforced HERE, not only at record time, so a revoked award frees its slot
    and an over-cap row that slipped a gate can never pay out)."""
    per: dict = {}
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
        slot = entry["bonus"].setdefault(
            rule_id, {"type": rule_type, "count": 0, "awarded": 0, "points": 0})
        slot["count"] += 1
        cap = rule.max_awards if rule is not None else 1
        if slot["awarded"] >= cap:
            continue  # over the per-player cap — dead weight, pays nothing
        # The row's quantity IS the points at award time; a rule edit while
        # drafting doesn't rewrite history (configs lock at activation).
        slot["awarded"] += 1
        slot["points"] += quantity
        entry["bonus_points"] += quantity
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
    return "Bonus"


def bonus_detail(rule_id: int, config: CompetitionConfig,
                 awarded_n: int, matched_target=None,
                 time_text: Optional[str] = None) -> dict:
    """Notification payload for one bonus award (``event_competition_bonus``):
    everything the message layout needs, pre-worded. ``time_text`` is the
    kill time recovered from the ledger note's human half (``bonus:… | 0:52.6``)
    — the row is the only durable carrier of it."""
    rule = config.rules_by_id.get(int(rule_id))
    if rule is None:
        return {"rule_id": int(rule_id), "label": "Bonus", "points": 0,
                "cap_line": None, "type": None}
    if rule.type == "time_under" and time_text:
        reason = (f"{(rule.npc or '').title()} in {time_text} "
                  f"(under {format_time_ms(rule.threshold_ms)})")
    elif rule.type == "pet" and matched_target:
        reason = f"New pet: {str(matched_target).strip()}"
    else:
        reason = rule_label(rule)
    cap_line = (f"Award {min(awarded_n, rule.max_awards)} of {rule.max_awards}"
                if rule.max_awards > 1 else None)
    return {
        "rule_id": rule.id,
        "type": rule.type,
        "points": rule.points,
        "label": rule_label(rule),
        "reason": reason,
        "cap_line": cap_line,
        "max_awards": rule.max_awards,
        "awarded_n": min(awarded_n, rule.max_awards),
    }
