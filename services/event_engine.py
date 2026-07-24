"""Event completion engine — shared logic (backend Task 17, events-prd.md A2/A3).

Three layers live here so the async worker (``workers/event_consumer.py``) and
the web_api confirmation flow (Task 18) share one implementation:

1. **Producer helper** — :func:`queue_submission` is the only thing the intake
   hot path touches: one gated, fire-and-forget ``LPUSH events:submissions``.
   No DB queries, never raises.
2. **Pure matcher** — :func:`match_task` & friends evaluate one submission
   envelope against one task dict. No I/O; unit-testable in isolation.
3. **Apply layer** — :func:`handle_envelope` / :func:`apply_ledger_row` fold a
   qualifying submission into the ``web_event_*`` tables (ledger, progress,
   team score, bingo cells), publish ``rt:event:{id}`` SSE frames and enqueue
   ``notification_queue`` rows. Callers own the session/commit.

Envelope (v1)::

    { "v": 1, "kind": "drop|pb|clog|ca|experience|pet",
      "guid": "...", "player_id": 123, "player_name": "...",
      "ts": 1751600000, "used_api": true, "data": { ...type-specific... } }

``used_api`` mirrors the submission's intake path (plugin API vs the
webhook-bot fallback); events gate on it via ``submission_policy``
(``all`` / ``confirm_non_api`` / ``api_only``). Absent (pre-upgrade queue
entries) is treated as non-API.

v1 evaluation semantics (task doc table):

- ``item_collection`` — drop/clog item name match (case-insensitive; config
  kinds ``any_of``/``all_of``/``point_collection``/``assembly`` best-effort:
  every listed item name matches; ``point_collection`` credits the item's
  point weight; ``groups`` combines all-of/any-of sub-requirements — every
  group must be satisfied, e.g. all godsword shards + any one hilt).
  Progress unit = quantity; ``any_of`` completes at ``target_value``
  qualifying drops (default 1). ``any_path`` paths may also be METRIC paths
  (``{"metric": "kc"|"loot_value", "npcs": [...], "need": N}``) — "boss pet
  OR 5,000 GWD kills"; each metric path folds its own tagged ledger rows
  (``note`` = ``path:<idx>``, guid suffixed ``#p<idx>``) with kc_target-style
  watermark dedupe / loot_value GP fold, and one submission may advance an
  item path and several metric paths at once (:func:`match_task_all`). A
  POINTS path (``{"kind": "points", "items": [{item_name, points}], "need":
  N}``) is the ``point_collection`` mode as an either-or branch — "Full set
  OR 500 pts of listed items" — folding its listed items' weighted quantities
  from the shared untagged rows toward a points goal.
  DT2 vestiges: a Gold ring dropped by the vestige's boss credits a task
  that lists the vestige AS that vestige, one unit per drop (2-ring stacks
  are the same roll chain), deduped once per (task, team, player, vestige)
  across rings/vestige/clog (:data:`VESTIGE_BOSSES`).
- ``kc_target`` — drop from the target NPC (or ANY of ``config.npcs`` on a
  multi-NPC task); each qualifying kill counts once (deduped by
  ``(npc, kill_count)`` per player via Redis; multi-NPC tasks keep their
  absolute-KC watermarks per NPC).
- ``pb_target`` — pb for the target boss with time ≤ target_value seconds;
  completes on first match.
- ``xp_target`` — xp gained in the target skill since the first report after
  join (per-player baseline in Redis ``events:{eid}:xpbase:{pid}:{skill}``).
- ``skill_target`` — experience report for the target skill with
  level ≥ target_value; completes on first match.
- ``pet_collection`` — pet submission (``kind == "pet"``). A ``target`` names
  one specific pet; ``config.pets`` (canonical names) is an explicit allow
  list (a category preset the builder customized — misc pets count when
  listed); otherwise ``config.categories`` (a list like ``["boss"]``) gates
  by pet category via :mod:`utils.osrs_pets`, and an absent/empty list means
  "any pet" (the default set, misc excluded).
  Progress unit = 1 per new pet; ``target_value`` is the count to collect
  (default 1 → completes on the first qualifying pet).
- ``ehp_target``/``ehb_target``/``custom`` — not auto-evaluated (Task 18
  manual/confirmation only).

Bingo (Task 20): cells are completed by the apply layer; after each newly
completed cell :func:`evaluate_bingo_bonuses` awards line/blackout bonuses.
The awarded set is derived idempotently from ``source_type='bonus'`` ledger
rows whose deterministic ``note`` names the line (``line:r3`` / ``line:c1`` /
``line:d0`` / ``blackout``) — no separate state. :func:`revoke_ledger_row`
unwinds cell completions and stale bonus rows when a completion is revoked.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import DataError, IntegrityError

# ── Redis keys / channels ─────────────────────────────────────────────────────
QUEUE_KEY = "events:submissions"           # LPUSH by producers, BRPOP by worker
# Batch 2: WOM-reconciler synthetic envelopes ride a lower-priority queue —
# a reconcile burst (one envelope per skill/boss per updated player) must
# never head-of-line-block live plugin drops. The consumer drains it only
# when the main queue is idle.
WOM_QUEUE_KEY = "events:submissions:wom"
ACTIVE_EVENTS_KEY = "events:active"        # set of active event ids (gate)
# P1-6: the gate carries a TTL refreshed by the consumer every ≤30s
# (STATE_REFRESH_SECONDS). If the consumer dies, the gate expires within this
# window and producers stop pushing — otherwise a dead consumer + an active
# event would LPUSH events:submissions unboundedly (a Redis-memory incident).
ACTIVE_EVENTS_TTL_SECONDS = 180
ADMIN_BUMP_CHANNEL = "rt:event-admin"      # pubsub bump on event/task/roster mutations

_STATE_KEY_TTL = 60 * 60 * 24 * 60         # 60 days for xp-baseline / kc-dedupe keys

# Task types the engine can evaluate automatically (v1).
AUTO_TASK_TYPES = ("item_collection", "kc_target", "pb_target", "xp_target", "skill_target",
                   "loot_value", "pet_collection", "loot_sweep")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Producer helper (intake hot path — tiny, guarded, never raises)
# ══════════════════════════════════════════════════════════════════════════════

def queue_submission(kind: str, player_id, guid, data: dict,
                     world_type: str = "main", player_name: str = None,
                     ts: int = None, used_api: bool = False) -> None:
    """Fire-and-forget push of a processed submission onto the events queue.

    Gated on the ``events:active`` Redis set (maintained by the worker) so it
    is a single O(1) EXISTS when no events run. Main world only (v1). Any
    Redis hiccup is swallowed — this must never fail a submission.
    """
    if world_type != "main":
        return
    try:
        from utils.redis import redis_client
        conn = getattr(redis_client, "client", None)
        if conn is None:
            return
        if not conn.exists(ACTIVE_EVENTS_KEY):
            return
        envelope = {
            "v": 1,
            "kind": kind,
            "guid": str(guid) if guid else None,
            "player_id": int(player_id),
            "ts": int(ts or time.time()),
            "used_api": bool(used_api),
            "data": data or {},
        }
        if player_name:
            envelope["player_name"] = str(player_name)
        conn.lpush(QUEUE_KEY, json.dumps(envelope, default=str))
    except Exception:
        pass


def publish_event_admin_bump(event_id: Optional[int] = None) -> None:
    """Tiny helper web_api calls on event/task/roster mutations so the worker
    refreshes its matcher state immediately (Task 18 wires the call sites)."""
    try:
        from utils.redis import redis_client
        conn = getattr(redis_client, "client", None)
        if conn is None:
            return
        conn.publish(ADMIN_BUMP_CHANNEL, json.dumps(
            {"event_id": event_id, "ts": int(time.time())}))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# 2. Pure matcher (no I/O)
# ══════════════════════════════════════════════════════════════════════════════

BLACKOUT_NOTE = "blackout"


def line_defs(size: int) -> dict:
    """{line_key: frozenset(cell idxs)} for every row/column/diagonal of a
    ``size`` × ``size`` board. Keys: ``r{i}`` (row i), ``c{i}`` (column i),
    ``d0`` (main diagonal), ``d1`` (anti-diagonal). Pure; no I/O."""
    n = int(size)
    lines = {}
    for r in range(n):
        lines[f"r{r}"] = frozenset(r * n + c for c in range(n))
    for c in range(n):
        lines[f"c{c}"] = frozenset(r * n + c for r in range(n))
    lines["d0"] = frozenset(i * n + i for i in range(n))
    lines["d1"] = frozenset(i * n + (n - 1 - i) for i in range(n))
    return lines


def completed_lines(size: int, completed_idxs) -> list:
    """Sorted line keys (see :func:`line_defs`) fully covered by
    ``completed_idxs``. Pure; no I/O."""
    done = set(completed_idxs)
    return sorted(key for key, cells in line_defs(size).items() if cells <= done)


def _norm(value) -> str:
    """Normalize a name for case-insensitive comparison."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _kc_npcs(task: dict) -> tuple:
    """Normalized NPC names a ``kc_target`` counts: the single ``target``,
    extended by ``config.npcs`` (multi-NPC tasks — a kill of ANY listed NPC
    advances the one shared counter). Precomputed as ``kc_npcs`` on state-load
    task dicts; derived on the fly otherwise (hand-built dicts in tests)."""
    pre = task.get("kc_npcs")
    if pre is not None:
        return tuple(pre)
    names = [task.get("target")]
    config = task.get("config") or {}
    if isinstance(config, dict):
        names.extend(config.get("npcs") or [])
    out: list = []
    for name in names:
        norm = _norm(name)
        if norm and norm not in out:
            out.append(norm)
    return tuple(out)


def _kc_wom_metrics(task: dict) -> dict:
    """``{wom metric slug -> normalized NPC name}`` for a ``kc_target``.
    Precomputed as ``wom_metrics`` at state-load; falls back to the legacy
    single ``wom_metric`` field (pre-upgrade dicts / tests)."""
    metrics = task.get("wom_metrics")
    if isinstance(metrics, dict):
        return metrics
    metric = task.get("wom_metric")
    if metric:
        return {metric: _norm(task.get("target"))}
    return {}


def _kc_state_scope(task: dict, npc_norm: str):
    """Redis key scope for a kc task's absolute-KC state (watermark / dedupe /
    fallback counters). Single-NPC tasks keep the bare task id — the legacy
    key shape, so live state survives an upgrade — while multi-NPC tasks track
    per NPC (each NPC has its own independent kill counter, so one shared
    watermark would swallow the smaller counts)."""
    if len(_kc_npcs(task)) <= 1:
        return task["id"]
    return f"{task['id']}:{npc_norm.replace(' ', '_')}"


# ── any_path metric paths (v2: "boss pet OR 5,000 KC") ────────────────────────

# Path metrics an any_path config may carry besides item groups. ``kc`` counts
# kills of the path's NPCs (kc_target semantics: watermark + dedupe, WOM
# reconciliation); ``loot_value`` folds drop GP (optionally NPC-scoped).
PATH_METRICS = ("kc", "loot_value")

_PATH_NOTE_PREFIX = "path:"


def _path_note(idx: int) -> str:
    """Engine-written ledger tag binding a metric row to its config path."""
    return f"{_PATH_NOTE_PREFIX}{int(idx)}"


def _row_path_idx(row) -> Optional[int]:
    """Metric-path index a ledger row was recorded for, parsed from the
    engine's ``note`` tag (``path:N``); None for item/manual/bonus rows.
    Reject/revoke may overwrite ``note`` with an admin note — those rows are
    already excluded from every fold, so the tag never needs to survive."""
    note = getattr(row, "note", None)
    if not isinstance(note, str) or not note.startswith(_PATH_NOTE_PREFIX):
        return None
    try:
        return int(note[len(_PATH_NOTE_PREFIX):])
    except (TypeError, ValueError):
        return None


def _path_need(path: dict) -> int:
    try:
        return max(int(path.get("need") or 0), 1)
    except (TypeError, ValueError):
        return 1


def _metric_paths_from_config(config: dict, resolve_wom: bool = False) -> tuple:
    """Normalized metric-path entries of an ``any_path`` config:
    ``({"idx", "metric", "npcs": frozenset, "need"[, "wom_metrics"]}, ...)``.
    ``idx`` is the position in ``config.paths`` (item paths keep their slots).
    ``resolve_wom`` additionally maps each kc path's NPCs to WOM hiscores
    metrics (state-load only — it does name lookups)."""
    out = []
    for idx, path in enumerate((config or {}).get("paths") or []):
        if not isinstance(path, dict) or path.get("metric") not in PATH_METRICS:
            continue
        entry = {"idx": idx, "metric": path["metric"],
                 "npcs": _norm_npc_set(path.get("npcs")), "need": _path_need(path)}
        if resolve_wom and path["metric"] == "kc":
            entry["wom_metrics"] = _task_wom_metrics("kc_target", entry["npcs"])
        out.append(entry)
    return tuple(out)


def _metric_paths(task: dict) -> tuple:
    """The task's metric paths (precomputed as ``metric_paths`` at state load
    — with WOM metrics resolved; derived on the fly for hand-built dicts,
    without them, mirroring ``_kc_npcs``). Empty for non-any_path tasks."""
    pre = task.get("metric_paths")
    if pre is not None:
        return tuple(pre)
    if _list_kind(task) != "any_path":
        return ()
    return _metric_paths_from_config(task.get("config") or {})


def _match_kc_scope(task: dict, match: dict, npc_norm: str):
    """Redis key scope for the absolute-KC state behind a kc/kc_abs match.
    kc_target tasks keep their legacy shapes (:func:`_kc_state_scope`); a
    metric-path match scopes per (task, path, NPC) — each path is its own
    counter and each NPC keeps its own independent watermark."""
    path_idx = match.get("path")
    if path_idx is None:
        return _kc_state_scope(task, npc_norm)
    return f"{task['id']}:p{int(path_idx)}:{npc_norm.replace(' ', '_')}"


def _match_wom_npc(task: dict, match: dict, metric: str) -> str:
    """Normalized NPC name behind a ``wom_kc`` envelope's boss metric, resolved
    against the matched scope (the task's map, or the matched path's)."""
    path_idx = match.get("path")
    if path_idx is None:
        return _kc_wom_metrics(task).get(metric) or ""
    for path in _metric_paths(task):
        if path["idx"] == path_idx:
            return (path.get("wom_metrics") or {}).get(metric) or ""
    return ""


def parse_task_config(raw) -> dict:
    """Parse an ``EventTask.config`` JSON payload; {} on any failure."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _config_item_entries(config: dict) -> dict:
    """Map normalized item name -> item entry (dict or None) from a task
    config. Supports ``items: [{item_name|name, quantity?, points?}]``, a
    bare ``any_of: [str, ...]`` list, and ``groups`` sub-requirement lists."""
    entries: dict = {}
    for it in (config.get("items") or []):
        if isinstance(it, str):
            name = _norm(it)
            if name:
                entries[name] = None
        elif isinstance(it, dict):
            name = _norm(it.get("item_name") or it.get("name"))
            if name:
                entries[name] = it
    for it in (config.get("any_of") or []):
        if isinstance(it, str):
            name = _norm(it)
            if name:
                entries.setdefault(name, None)
        elif isinstance(it, dict):
            name = _norm(it.get("item_name") or it.get("name"))
            if name:
                entries.setdefault(name, it)
    def _add_group_items(groups) -> None:
        for group in (groups or []):
            if isinstance(group, dict):
                for it in (group.get("items") or []):
                    name = _norm(it if isinstance(it, str)
                                 else (it or {}).get("item_name") or (it or {}).get("name"))
                    if name:
                        entries.setdefault(name, None)

    _add_group_items(config.get("groups"))
    for path in (config.get("paths") or []):
        if isinstance(path, dict):
            _add_group_items(path.get("groups"))
            # Points paths (any_path alternative: "500 pts of listed items")
            # carry a flat weighted item list — register the names so the base
            # item match fires. The weight is applied at fold time, not here.
            for it in (path.get("items") or []):
                name = _norm(it if isinstance(it, str)
                             else (it or {}).get("item_name") or (it or {}).get("name"))
                if name:
                    entries.setdefault(name, None)
    return entries


def item_match_quantity(task: dict, item_name, quantity=1) -> Optional[int]:
    """Credit quantity if ``item_name`` satisfies an ``item_collection`` task,
    else None. ``point_collection`` configs credit the item's point weight
    (best-effort, rounded, min 1)."""
    name = _norm(item_name)
    if not name:
        return None
    try:
        qty = max(int(quantity or 1), 1)
    except (TypeError, ValueError):
        qty = 1

    if _norm(task.get("target")) == name:
        return qty

    config = task.get("config") or {}
    entries = _config_item_entries(config)
    if name not in entries:
        return None
    entry = entries[name]
    if config.get("kind") == "point_collection" and isinstance(entry, dict):
        try:
            points = float(entry.get("points") or 1)
        except (TypeError, ValueError):
            points = 1.0
        return max(int(round(points * qty)), 1)
    return qty


def _config_pet_names(config: dict) -> frozenset:
    """Normalized names in an item-list config flagged as PETS
    (``config.pet_items``): they credit from a ``pet`` submission by name and
    are excluded from drop/clog matching. Tolerates garbage (empty set)."""
    raw = (config or {}).get("pet_items")
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(_norm(n) for n in raw if _norm(n))


def _task_pet_names(task: dict) -> frozenset:
    """The task's pet-flagged item names (precomputed at state load; derived
    on the fly for hand-built dicts, mirroring ``_item_source_npcs``)."""
    pets = task.get("pet_name_set")
    if pets is None:
        pets = _config_pet_names(task.get("config") or {})
    return pets


def _norm_npc_set(raw) -> frozenset:
    """Normalized NPC-name frozenset from a config value. Tolerates a
    non-list/garbage value (returns empty) so a hand-authored / seeded / template
    config with a malformed source restriction can never raise during state
    load and wedge the consumer — the web validator always emits list[str]."""
    if not isinstance(raw, (list, tuple, set)):
        return frozenset()
    return frozenset(_norm(n) for n in raw if _norm(n))


def _item_source_index(config: dict) -> dict:
    """``{normalized item name -> frozenset(normalized npc names)}`` for an
    ``item_collection`` task's per-item source restriction.

    The restriction lives in ``config.item_npcs`` (a ``{item_name: [npc, ...]}``
    map — a flat map rather than per-entry ``npcs`` so it works uniformly across
    every list kind, including ``groups``/``any_path`` whose stored item lists
    are bare name strings). An item absent from the index is UNRESTRICTED; only
    items with a non-empty NPC list are included."""
    out: dict = {}
    raw = (config or {}).get("item_npcs")
    if not isinstance(raw, dict):
        return out
    for iname, npcs in raw.items():
        key = _norm(iname)
        if not key:
            continue
        allowed = _norm_npc_set(npcs)
        if allowed:
            out[key] = out.get(key, frozenset()) | allowed
    return out


def _item_source_npcs(task: dict, item_name) -> frozenset:
    """The NPC names a dropped ``item_name`` must come from to credit this
    ``item_collection`` task, or an empty set when unrestricted.

    Per-item ``config.item_npcs`` wins; a single-item task falls back to the
    task-level ``config.source_npcs`` (the drop only ever matches ``target``
    there). Precomputed as ``item_source_index`` / ``task_source_npcs`` at
    state-load; derived on the fly for hand-built dicts (tests), mirroring the
    ``_kc_npcs`` pattern."""
    idx = task.get("item_source_index")
    task_src = task.get("task_source_npcs")
    if idx is None and task_src is None:
        config = task.get("config") or {}
        idx = _item_source_index(config)
        task_src = _norm_npc_set(config.get("source_npcs"))
    allowed = (idx or {}).get(_norm(item_name))
    if allowed:
        return allowed
    return task_src or frozenset()


# ── DT2 vestige pity rolls ("gold ring" progress drops) ──────────────────────
# A vestige takes three successful invisible rolls; the first two drop 1× and
# 2× Gold ring instead of the vestige. For events, a ring dropped by the
# vestige's boss counts AS that vestige when the task lists it (hitting the
# roll is the achievement) — one unit regardless of stack size (the 2-ring
# stack is the SAME vestige's second roll), deduped so one player's
# ring/ring/vestige chain credits a task once (_dedupe_vestige_chain).
VESTIGE_RING_NAME = "gold ring"
# Canonical vestige display name -> normalized names of the one boss that
# drops it (awakened variants included). Verified against recorded drops.
VESTIGE_BOSSES = {
    "Ultor vestige": frozenset({"vardorvis", "vardorvis (awakened)"}),
    "Magus vestige": frozenset({"duke sucellus", "duke sucellus (awakened)"}),
    "Venator vestige": frozenset({"the leviathan", "leviathan (awakened)"}),
    "Bellator vestige": frozenset({"the whisperer", "whisperer (awakened)",
                                   "the whisperer (awakened)"}),
}
_VESTIGE_NORMS = frozenset(name.lower() for name in VESTIGE_BOSSES)


def _ring_vestige_for_task(task: dict, item_name, npc_name) -> Optional[str]:
    """The vestige display name a Gold-ring DROP credits on this task, or
    None. Applies only when the ring itself isn't a listed item (the caller
    checks — a literal gold-ring task keeps normal stack semantics), the
    source NPC is a vestige's boss, and the task lists/targets that vestige."""
    if _norm(item_name) != VESTIGE_RING_NAME:
        return None
    npc = _norm(npc_name)
    if not npc:
        return None
    for display, bosses in VESTIGE_BOSSES.items():
        if npc in bosses and item_match_quantity(task, display, 1) is not None:
            return display
    return None


# pb_target completion requirements (config {"mode", "need"}):
# - "times":          the time must be beaten ``need`` times (every qualifying
#                     kill counts — teammates on one kill each count, a player
#                     may repeat; default, need 1 = the legacy first-match).
# - "unique_players": ``need`` DIFFERENT players must each beat it once.
# - "whole_team":     every rostered member of the team must beat it — the
#                     threshold is the team's size, resolved per team at apply
#                     time (:func:`effective_threshold`).
PB_COMPLETION_MODES = ("times", "unique_players", "whole_team")


def _pb_mode(task: dict) -> tuple:
    """``(mode, need)`` of a pb_target's completion requirement. Config-less
    tasks are ``("times", 1)`` — the legacy complete-on-first-match. Accepts
    raw (string) configs so route-built task dicts resolve correctly."""
    config = parse_task_config(task.get("config"))
    mode = config.get("mode")
    if mode not in PB_COMPLETION_MODES:
        mode = "times"
    try:
        need = max(int(config.get("need") or 1), 1)
    except (TypeError, ValueError):
        need = 1
    return mode, need


def completion_threshold(task: dict) -> int:
    """Progress value at which the task completes.

    ``whole_team`` pb tasks return 1 here — their real threshold is the
    team's roster size, which a pure task dict can't know. Every completion
    DECISION runs through :func:`effective_threshold` (apply / revoke /
    projection / manual award); this pure fallback only reaches display
    paths that lack a team."""
    if task.get("type") == "pb_target":
        mode, need = _pb_mode(task)
        return need if mode in ("times", "unique_players") else 1
    if task.get("type") == "skill_target":
        return 1
    try:
        return max(int(task.get("target_value") or 0), 1)
    except (TypeError, ValueError):
        return 1


def effective_threshold(session, task: dict, team_id) -> int:
    """:func:`completion_threshold`, resolving ``whole_team`` pb tasks against
    the team's actual roster: explicit ``EventTeamMember`` rows, else — for
    auto-clan fallback teams, which keep no roster rows — the clan's current
    member count (the same source the matcher credits from)."""
    if (task.get("type") != "pb_target" or team_id is None
            or _pb_mode(task)[0] != "whole_team"):
        return completion_threshold(task)
    from db.models import EventTeam, EventTeamMember

    n = (session.query(EventTeamMember)
         .filter(EventTeamMember.team_id == team_id).count())
    if not n:
        team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
        if team is not None and getattr(team, "auto_clan", False) and team.group_id:
            from db.models.associations import user_group_association

            n = (session.query(user_group_association.c.player_id)
                 .filter(user_group_association.c.group_id == team.group_id,
                         user_group_association.c.player_id.isnot(None))
                 .distinct().count())
    return max(int(n or 0), 1)


# Continuous-metric task types whose plugin progress fan-out is stepped: every
# XP drop / loot stack is an increment, so per-increment envelopes would spam
# every teammate's game chat. Discrete tasks (items, KC) stay per-increment.
PLUGIN_PROGRESS_STEP_TASK_TYPES = ("xp_target", "loot_value")
PLUGIN_PROGRESS_STEP_PCT = 10


def _plugin_progress_step_crossed(previous: int, current: int, threshold: int) -> bool:
    """True when previous→current crosses a PLUGIN_PROGRESS_STEP_PCT boundary
    of the threshold (integer math: which 10%-bucket each value sits in)."""
    if threshold <= 0:
        return True
    steps = 100 // PLUGIN_PROGRESS_STEP_PCT
    return (current * steps) // threshold > (previous * steps) // threshold


def _list_kind(task: dict) -> Optional[str]:
    """Item-list config kind (any_of/all_of/point_collection/assembly/groups/any_path), if any."""
    config = task.get("config") or {}
    return config.get("kind") if isinstance(config, dict) else None


def _pb_distinct_players(task: dict) -> bool:
    """Whether this pb task's rollup counts DISTINCT players (unique_players /
    whole_team) rather than folding qualifying-kill quantities."""
    return (task.get("type") == "pb_target"
            and _pb_mode(task)[0] in ("unique_players", "whole_team"))


def _distinct_item_progress(session, task: dict, team_id, include=None) -> int:
    """all_of/assembly rollup: one unit per DISTINCT listed item collected
    (quantity is irrelevant — a 1,338-coins drop is still just "Coins"), plus
    manual wildcard rows (no matched item) counting their quantity each,
    capped at the threshold so wildcard awards can't overshoot.

    ``include`` folds a row not yet visible to the applied-status query (the
    confirm flow applies before the caller flips ``pending`` → ``confirmed``).
    """
    from db.models import EventCompletion

    rows = list(
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(("auto", "confirmed", "manual")))
        .all()
    )
    if include is not None and all(r.id != include.id for r in rows):
        rows.append(include)
    return _distinct_progress_from_rows(rows, completion_threshold(task))


def _distinct_progress_from_rows(rows, threshold: int) -> int:
    """Pure core of :func:`_distinct_item_progress` (unit-testable)."""
    distinct: set = set()
    wildcard = 0
    for r in rows:
        if (getattr(r, "source_type", None) or "") == "bonus":
            continue
        target = getattr(r, "matched_target", None)
        if target:
            distinct.add(_norm(target))
        else:
            wildcard += max(int(getattr(r, "quantity", 1) or 1), 1)
    return min(len(distinct) + wildcard, threshold)


def _distinct_players_from_rows(rows, threshold: int) -> int:
    """One unit per DISTINCT contributing player (pb unique_players /
    whole_team rollups) — a grinder's tenth sub-threshold kill is still one
    player. Player-less manual wildcard rows count their quantity each (the
    admin mark-complete escape hatch), capped at the threshold."""
    players: set = set()
    wildcard = 0
    for r in rows:
        if (getattr(r, "source_type", None) or "") == "bonus":
            continue
        pid = getattr(r, "player_id", None)
        if pid is not None:
            players.add(pid)
        else:
            wildcard += max(int(getattr(r, "quantity", 1) or 1), 1)
    return min(len(players) + wildcard, threshold)


def _distinct_player_progress(session, task: dict, team_id, threshold: int,
                              include=None) -> int:
    """pb unique_players / whole_team rollup over the applied ledger
    (mirrors :func:`_distinct_item_progress`; ``include`` folds a row not yet
    visible to the applied-status query)."""
    from db.models import EventCompletion

    rows = list(
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(("auto", "confirmed", "manual")))
        .all()
    )
    if include is not None and all(r.id != include.id for r in rows):
        rows.append(include)
    return _distinct_players_from_rows(rows, threshold)


def _grouped_item_progress(session, task: dict, team_id, include=None) -> int:
    """``kind: "groups"`` rollup — each group is its own all-of (distinct
    listed items) or any-of (quantities fold, capped at ``need``) requirement,
    and overall progress is the sum of per-group progress. The task threshold
    is the sum of group needs, so it completes exactly when every group is
    satisfied. Manual wildcard rows count their quantity toward the total,
    like :func:`_distinct_item_progress`.
    """
    from db.models import EventCompletion

    rows = list(
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(("auto", "confirmed", "manual")))
        .all()
    )
    if include is not None and all(r.id != include.id for r in rows):
        rows.append(include)
    return _grouped_progress_from_rows(rows, task.get("config") or {},
                                       completion_threshold(task))


def _parse_requirement_groups(config: dict) -> list[tuple[str, set, int]]:
    """``groups`` config → normalized ``(mode, item-name set, need)`` tuples
    (shared by the grouped and any_path rollups)."""
    groups: list[tuple[str, set, int]] = []
    for group in (config.get("groups") or []):
        if not isinstance(group, dict):
            continue
        names = {
            _norm(it if isinstance(it, str)
                  else (it or {}).get("item_name") or (it or {}).get("name"))
            for it in (group.get("items") or [])
        }
        names.discard("")
        if not names:
            continue
        mode = group.get("mode") if group.get("mode") in ("all_of", "any_of") else "all_of"
        try:
            need = max(int(group.get("need") or 0), 1) if mode == "any_of" else len(names)
        except (TypeError, ValueError):
            need = 1
        groups.append((mode, names, need))
    return groups


def _grouped_progress_from_rows(rows, config: dict, threshold: int) -> int:
    """Pure core of :func:`_grouped_item_progress` (unit-testable)."""
    groups = _parse_requirement_groups(config)

    distinct: list[set] = [set() for _ in groups]
    folded = [0] * len(groups)
    wildcard = 0
    for r in rows:
        if (getattr(r, "source_type", None) or "") == "bonus":
            continue
        name = _norm(getattr(r, "matched_target", None))
        qty = max(int(getattr(r, "quantity", 1) or 1), 1)
        if not name:
            wildcard += qty
            continue
        for gi, (mode, names, _need) in enumerate(groups):
            if name in names:
                if mode == "all_of":
                    distinct[gi].add(name)
                else:
                    folded[gi] += qty
                break

    progress = wildcard
    for gi, (mode, _names, need) in enumerate(groups):
        got = len(distinct[gi]) if mode == "all_of" else folded[gi]
        progress += min(got, need)
    return min(progress, threshold)


def _anypath_item_progress(session, task: dict, team_id, include=None) -> int:
    """``kind: "any_path"`` rollup — each path is its own groups-style
    requirement set and the task completes when ANY path is fully satisfied
    ("dryness protection": the full Justiciar set OR any 5 Justiciar items).
    A drop advances every path that lists it, so paths race independently.
    """
    from db.models import EventCompletion

    rows = list(
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(("auto", "confirmed", "manual")))
        .all()
    )
    if include is not None and all(r.id != include.id for r in rows):
        rows.append(include)
    return _anypath_progress_from_rows(rows, task.get("config") or {},
                                       completion_threshold(task))


def _is_points_path(path) -> bool:
    """A POINTS path is an untagged any_path alternative whose flat item list
    carries per-item weights and a ``need`` points goal (``kind: "points"``) —
    the ``point_collection`` mode as an either-or branch ("Full set OR 500 pts
    of listed items"). Distinct from metric paths (tagged kc/loot_value) and
    from item-checklist paths (``groups``)."""
    return isinstance(path, dict) and path.get("kind") == "points"


def _path_point_weights(path: dict) -> dict:
    """``{normalized item name -> integer point weight (≥1)}`` for a points
    path (``items: [{item_name, points}]``). Bare-string items weigh 1."""
    out: dict = {}
    for it in (path.get("items") or []):
        if isinstance(it, dict):
            name = _norm(it.get("item_name") or it.get("name"))
            if not name:
                continue
            try:
                weight = int(round(float(it.get("points") or 1)))
            except (TypeError, ValueError):
                weight = 1
            out[name] = max(weight, 1)
        elif isinstance(it, str):
            name = _norm(it)
            if name:
                out[name] = 1
    return out


def _points_fold(rows, weights: dict) -> int:
    """Weighted point total of untagged item rows for a points path — each
    row's matched-item weight times its quantity (mirrors point_collection).
    Wildcard (no matched item) and bonus rows contribute nothing here; wildcard
    manual awards ride the task's percent scale in :func:`_anypath_progress_from_rows`."""
    total = 0
    for r in rows:
        if (getattr(r, "source_type", None) or "") == "bonus":
            continue
        name = _norm(getattr(r, "matched_target", None))
        if name and name in weights:
            total += weights[name] * max(int(getattr(r, "quantity", 1) or 1), 1)
    return total


def _anypath_progress_from_rows(rows, config: dict, threshold: int) -> int:
    """Pure core of :func:`_anypath_item_progress` (unit-testable).

    Paths differ in size, so the single rollup integer is the *percentage* of
    the closest-to-done path scaled to the threshold (validation pins
    target_value to 100). Floor rounding means the threshold is hit exactly
    when some path's own need is fully met, never one drop early.

    Metric paths (``{"metric": "kc"|"loot_value", "need": N}``) fold the
    quantities of their own tagged rows (``note`` = ``path:<idx>``); item and
    points paths fold only the untagged rows — so a 5,000-KC path can never
    leak kills into a sibling item checklist, or vice versa. A POINTS path
    (``{"kind": "points", "items": [...], "need": N}``) weights each untagged
    item row by its point value (like a metric goal on the percent scale).
    Untagged WILDCARD rows (manual awards, no matched item) count on the task's
    percent scale: item paths absorb them inside the grouped fold as before,
    and metric/points paths add them as percent points — which is what keeps
    the admin "mark complete" award (quantity = threshold − done) completing a
    metric-only or points-only task exactly like any other.
    """
    item_rows = []
    tagged: dict = {}
    wildcard = 0
    for r in rows:
        idx = _row_path_idx(r)
        if idx is None:
            item_rows.append(r)
            if ((getattr(r, "source_type", None) or "") != "bonus"
                    and not _norm(getattr(r, "matched_target", None))):
                wildcard += max(int(getattr(r, "quantity", 1) or 1), 1)
        elif (getattr(r, "source_type", None) or "") != "bonus":
            tagged[idx] = tagged.get(idx, 0) + max(int(getattr(r, "quantity", 1) or 1), 1)
    best = 0
    for pi, path in enumerate(config.get("paths") or []):
        if not isinstance(path, dict):
            continue
        if path.get("metric") in PATH_METRICS:
            need = _path_need(path)
            got = min(tagged.get(pi, 0), need)
            pct = min((got * threshold) // need + wildcard, threshold)
        elif _is_points_path(path):
            need = _path_need(path)
            got = min(_points_fold(item_rows, _path_point_weights(path)), need)
            pct = min((got * threshold) // need + wildcard, threshold)
        else:
            need = sum(n for _mode, _names, n in _parse_requirement_groups(path))
            if need <= 0:
                continue
            got = _grouped_progress_from_rows(item_rows, path, need)
            pct = (got * threshold) // need
        best = max(best, pct)
    return min(best, threshold)


def pending_projection(session, task: dict, team_id) -> Optional[dict]:
    """What a (task, team) would look like if its ``pending`` ledger rows were
    all confirmed (web53a pending-review board highlight; pure read).

    Returns ``{"applied", "projected", "pending_count", "pending_complete"}``
    — or None when the pair has no pending rows at all (the overwhelmingly
    common case; callers skip the overlay entirely). Mirrors the recompute
    rules in ``_apply``/``apply_revocation`` per config kind so the projection
    can never disagree with what confirming would actually produce.
    """
    from db.models import EventCompletion

    rows = list(
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(("auto", "confirmed", "manual", "pending")))
        .all()
    )
    pending_rows = [r for r in rows if r.status == "pending"]
    if not pending_rows:
        return None
    applied_rows = [r for r in rows if r.status != "pending"]
    threshold = effective_threshold(session, task, team_id)
    kind = _list_kind(task)
    if _pb_distinct_players(task):
        return {
            "applied": _distinct_players_from_rows(applied_rows, threshold),
            "projected": _distinct_players_from_rows(rows, threshold),
            "pending_count": len(pending_rows),
            "pending_complete": _distinct_players_from_rows(rows, threshold) >= threshold,
        }
    if kind == "loot_sweep":
        # "applied"/"projected" are running POINT totals here (loot_sweep never
        # completes, so pending_complete is always False).
        from services.loot_sweep import LootSweepConfig, team_total
        cfg = LootSweepConfig(task.get("config") or {})
        return {
            "applied": team_total(applied_rows, cfg),
            "projected": team_total(rows, cfg),
            "pending_count": len(pending_rows),
            "pending_complete": False,
        }
    if kind in ("all_of", "assembly"):
        applied = _distinct_progress_from_rows(applied_rows, threshold)
        projected = _distinct_progress_from_rows(rows, threshold)
    elif kind == "groups":
        config = task.get("config") or {}
        applied = _grouped_progress_from_rows(applied_rows, config, threshold)
        projected = _grouped_progress_from_rows(rows, config, threshold)
    elif kind == "any_path":
        config = task.get("config") or {}
        applied = _anypath_progress_from_rows(applied_rows, config, threshold)
        projected = _anypath_progress_from_rows(rows, config, threshold)
    else:
        def _fold(subset):
            return sum(
                max(int(r.quantity or 1), 1) for r in subset
                if (getattr(r, "source_type", None) or "") != "bonus"
            )
        applied = _fold(applied_rows)
        projected = applied + _fold(pending_rows)
    return {
        "applied": applied,
        "projected": projected,
        "pending_count": len(pending_rows),
        "pending_complete": projected >= threshold,
    }


def match_task(task: dict, envelope: dict) -> Optional[dict]:
    """Match one envelope against one task dict (pure; no I/O).

    Returns None or ``{"mode": ..., "quantity": ...}`` where mode is:

    - ``count`` — fold quantity into progress
    - ``kc``    — fold 1 kill, after per-player (npc, kill_count) dedupe
    - ``first`` — complete on first ledger row (pb_target / skill_target)
    - ``xp``    — quantity is the Redis-baselined xp delta (resolved later)
    """
    task_type = task.get("type")
    kind = envelope.get("kind")
    data = envelope.get("data") or {}

    if task_type == "item_collection":
        if kind not in ("drop", "clog", "pet"):
            return None
        pets = _task_pet_names(task)
        if kind == "pet":
            # A pet mixed into the item list (config.pet_items) credits from
            # its pet submission by name — mirroring loot_sweep's pet entries.
            raw_name = data.get("pet_name") or data.get("item_name")
            if not raw_name or _norm(raw_name) not in pets:
                return None
            credit = item_match_quantity(task, raw_name, 1)
            if credit is None:
                return None
            return {"mode": "count", "quantity": credit,
                    "matched_target": str(raw_name).strip()[:120] or None}
        item_name = data.get("item_name")
        # A pet-flagged name only ever credits from its `pet` submission — a
        # same-named drop/clog row would double-credit alongside it.
        if pets and _norm(item_name) in pets:
            return None
        qty = data.get("quantity", 1) if kind == "drop" else 1
        credit = item_match_quantity(task, item_name, qty)
        if credit is None:
            # DT2 pity rolls: a Gold ring DROPPED by a vestige's boss credits
            # the task AS that vestige — one unit regardless of stack size
            # (the 2-ring stack is the same vestige's second roll, not two).
            # Only when the ring itself isn't listed: a literal gold-ring
            # task keeps normal quantity semantics.
            vestige = (_ring_vestige_for_task(task, item_name, data.get("npc_name"))
                       if kind == "drop" else None)
            if vestige is None or (pets and _norm(vestige) in pets):
                return None
            item_name = vestige
            credit = item_match_quantity(task, vestige, 1)
            if credit is None:
                return None
        # Optional source-NPC restriction (config.source_npcs / config.item_npcs).
        # When set for this item, only a DROP from a listed NPC credits it — a
        # clog carries a source string but is never a "drop from this NPC", so a
        # restricted item can't be satisfied by a collection-log unlock.
        allowed = _item_source_npcs(task, item_name)
        if allowed and (kind != "drop" or _norm(data.get("npc_name")) not in allowed):
            return None
        # The matched name rides along to the ledger row so all_of/assembly
        # progress can count DISTINCT items rather than folding quantities.
        return {"mode": "count", "quantity": credit,
                "matched_target": str(item_name or "").strip()[:120] or None}

    if task_type == "loot_sweep":
        # Loot Sweep (v2). ``loot_sweep_index`` (item key -> {source, npcs}) is
        # precomputed on the task dict in _task_to_dict. A "drop" item only
        # credits when it DROPS from its group's target NPC; a "pet" item
        # credits from a `pet` submission matched by name (a pet only comes from
        # its boss, so no NPC scoping). Decaying scoring happens at apply time.
        index = task.get("loot_sweep_index") or {}
        if kind == "drop":
            name = _norm(data.get("item_name"))
            entry = index.get(name)
            if entry is None or entry.get("source") != "drop":
                return None
            allowed = entry.get("npcs")
            if allowed and _norm(data.get("npc_name")) not in allowed:
                return None
            raw_name = data.get("item_name")
            try:
                qty = max(int(data.get("quantity", 1) or 1), 1)
            except (TypeError, ValueError):
                qty = 1
        elif kind == "pet":
            raw_name = data.get("pet_name") or data.get("item_name")
            name = _norm(raw_name)
            entry = index.get(name)
            if entry is None or entry.get("source") != "pet":
                return None
            qty = 1
        else:
            return None
        return {"mode": "count", "quantity": qty,
                "matched_target": str(raw_name or "").strip()[:120] or None}

    if task_type == "kc_target":
        if kind == "wom_kc":
            # WOM reconciler envelope: absolute boss KC keyed by WOM metric
            # slug (precomputed on the task dict at state-load time).
            # Quantity (the watermark delta) is resolved later, like xp.
            metric = str(data.get("boss_metric") or "").strip().lower()
            if not metric or metric not in _kc_wom_metrics(task):
                return None
            return {"mode": "kc_abs", "quantity": 0}
        if kind != "drop":
            return None
        # A kill of ANY of the task's NPCs counts (config.npcs extends the
        # single target — e.g. "50 Dagannoth Kings" across Rex/Prime/Supreme).
        npcs = _kc_npcs(task)
        if not npcs or _norm(data.get("npc_name")) not in npcs:
            return None
        return {"mode": "kc", "quantity": 1}

    if task_type == "pb_target":
        if kind != "pb":
            return None
        if _norm(data.get("npc_name")) != _norm(task.get("target")) or not task.get("target"):
            return None
        try:
            time_ms = int(data.get("time_ms") or 0)
            target_seconds = int(task.get("target_value") or 0)
        except (TypeError, ValueError):
            return None
        if time_ms <= 0 or target_seconds <= 0 or time_ms > target_seconds * 1000:
            return None
        # Legacy single-shot completes on the first qualifying kill; the
        # times / unique_players / whole_team requirements count every
        # qualifying kill (teammates on one kill each count — the rollup
        # decides whether repeats by one player advance progress).
        mode, need = _pb_mode(task)
        if mode == "times" and need <= 1:
            return {"mode": "first", "quantity": 1}
        return {"mode": "count", "quantity": 1}

    if task_type == "xp_target":
        if kind != "experience":
            return None
        if _norm(data.get("skill")) != _norm(task.get("target")) or not task.get("target"):
            return None
        return {"mode": "xp", "quantity": 0}

    if task_type == "loot_value":
        # Accumulate GP from drops, optionally scoped to specific NPCs via
        # ``target`` (single) and/or ``config.source_npcs`` (list).
        if kind != "drop":
            return None
        try:
            value = int(data.get("total_value") or 0)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        config = task.get("config") or {}
        sources = {_norm(n) for n in (config.get("source_npcs") or []) if _norm(n)}
        if task.get("target"):
            sources.add(_norm(task["target"]))
        if sources and _norm(data.get("npc_name")) not in sources:
            return None
        return {"mode": "count", "quantity": value}

    if task_type == "skill_target":
        if kind != "experience":
            return None
        if _norm(data.get("skill")) != _norm(task.get("target")) or not task.get("target"):
            return None
        try:
            level = int(data.get("level") or 0)
            target_level = int(task.get("target_value") or 0)
        except (TypeError, ValueError):
            return None
        if target_level <= 0 or level < target_level:
            return None
        return {"mode": "first", "quantity": 1}

    if task_type == "pet_collection":
        if kind != "pet":
            return None
        pet_name = data.get("pet_name") or data.get("item_name")
        if not pet_name:
            return None
        target = task.get("target")
        if target:
            # Specific pet: exact name match (any category, incl. misc).
            if _norm(pet_name) != _norm(target):
                return None
        elif (task.get("config") or {}).get("pets"):
            # Explicit pet list (category preset the builder customized):
            # name membership, like N specific pets. Misc pets count when
            # listed — listing one is as deliberate as targeting it.
            listed = (task.get("config") or {}).get("pets")
            if _norm(pet_name) not in {_norm(p) for p in listed}:
                return None
        else:
            # Category / any-pet: membership resolved from the live taxonomy.
            from utils.osrs_pets import pet_matches
            categories = (task.get("config") or {}).get("categories")
            if not pet_matches(pet_name, categories):
                return None
        return {"mode": "count", "quantity": 1,
                "matched_target": str(pet_name).strip()[:120] or None}

    # ehp_target / ehb_target / custom: manual-confirmation only (Task 18).
    return None


def match_task_all(task: dict, envelope: dict) -> list:
    """Every match one envelope produces against one task (pure; no I/O).

    Single-outcome tasks defer to :func:`match_task`. An ``any_path`` task
    with metric paths can credit several paths from ONE submission — the drop
    that advances an item path is also a kill for a KC path and GP for a
    loot-value path ("paths race independently") — so each qualifying metric
    path yields its own match dict carrying ``path`` (the config path index
    the apply layer tags the ledger row with).
    """
    matches = []
    base = match_task(task, envelope)
    if base is not None:
        matches.append(base)
    metric_paths = _metric_paths(task)
    if not metric_paths:
        return matches
    kind = envelope.get("kind")
    data = envelope.get("data") or {}
    if kind == "drop":
        npc = _norm(data.get("npc_name"))
        try:
            value = int(data.get("total_value") or 0)
        except (TypeError, ValueError):
            value = 0
        for path in metric_paths:
            if path["metric"] == "kc":
                if npc and npc in path["npcs"]:
                    matches.append({"mode": "kc", "quantity": 1, "path": path["idx"]})
            elif path["metric"] == "loot_value" and value > 0:
                if path["npcs"] and npc not in path["npcs"]:
                    continue
                matches.append({"mode": "count", "quantity": value, "path": path["idx"]})
    elif kind == "wom_kc":
        metric = str(data.get("boss_metric") or "").strip().lower()
        if metric:
            for path in metric_paths:
                if path["metric"] == "kc" and metric in (path.get("wom_metrics") or {}):
                    matches.append({"mode": "kc_abs", "quantity": 0, "path": path["idx"]})
    return matches


def accepts_submission_source(event: dict, envelope: dict) -> bool:
    """Whether the event's submission_policy admits this envelope's intake
    path (pure; no I/O). Only ``api_only`` rejects; a missing ``used_api``
    flag (pre-upgrade queue entries, webhook-bot fallback) reads as non-API.
    WOM-reconciler envelopes are hiscores-sourced server-side data — trusted
    under every policy."""
    if envelope.get("source") == "wom":
        return True
    if event.get("submission_policy") == "api_only":
        return bool(envelope.get("used_api"))
    return True


def completion_status(event: dict, task: dict, envelope: dict) -> str:
    """Initial ledger-row status for a match (pure; no I/O): ``pending`` when
    the task or event forces confirmation (PRD D3), or when the event's
    ``confirm_non_api`` policy holds a submission that didn't arrive via the
    plugin API; ``auto`` otherwise."""
    if task.get("requires_confirmation") or event.get("requires_confirmation"):
        return "pending"
    if (event.get("submission_policy") == "confirm_non_api"
            and not envelope.get("used_api")
            and envelope.get("source") != "wom"):
        return "pending"
    return "auto"


# ══════════════════════════════════════════════════════════════════════════════
# Matcher state (loaded by the worker every 30s / on admin bump)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MatcherState:
    """In-memory snapshot of active events; keeps per-submission handling
    query-free until a match is found."""
    events: dict = field(default_factory=dict)          # event_id -> event dict
    tasks_by_event: dict = field(default_factory=dict)  # event_id -> [task dict]
    cells_by_task: dict = field(default_factory=dict)   # task_id -> [cell dict]
    participants: dict = field(default_factory=dict)    # player_id -> [(event_id, team_id, joined_at)]
    team_names: dict = field(default_factory=dict)      # team_id -> name
    # Board-game turn pointers (web44a): (event_id, team_id) -> the ONE task
    # id the matcher may evaluate for that team. Absent key = no live task
    # (awaiting roll / blocked / finished) — submissions are ignored.
    board_task_by_team: dict = field(default_factory=dict)
    loaded_at: float = 0.0


def _event_to_dict(event) -> dict:
    # Effective window (PRD D10/A5): scheduled dates narrowed by explicit
    # activate/end actions. Evaluation is frozen outside it.
    starts = [d for d in (event.starts_at, event.activated_at) if d is not None]
    ends = [d for d in (event.ends_at, event.ended_at) if d is not None]
    return {
        "id": event.id,
        "name": event.name,
        "group_id": event.group_id,
        "requires_confirmation": bool(event.requires_confirmation),
        "submission_policy": getattr(event, "submission_policy", None) or "all",
        "message_config": getattr(event, "message_config", None),
        "has_bingo": bool(event.has_bingo),
        "kind": getattr(event, "kind", None) or "standard",
        "board_size": int(event.board_size or 5),
        "bonus_line_points": int(event.bonus_line_points or 0),
        "bonus_blackout_points": int(event.bonus_blackout_points or 0),
        "window_start": max(starts) if starts else None,
        "window_end": min(ends) if ends else None,
    }


def _task_wom_metrics(task_type, npcs) -> dict:
    """``{wom metric slug -> normalized NPC name}`` for a kc_target's NPC set
    (NPCs without a WOM hiscores metric are absent — those stay plugin-only)."""
    if task_type != "kc_target" or not npcs:
        return {}
    out: dict = {}
    for npc in npcs:
        try:
            from utils.wiseoldman import wom_boss_metric
            slug = wom_boss_metric(npc)
        except Exception:
            slug = None
        if slug:
            out[slug] = npc
    return out


def _task_to_dict(task) -> dict:
    d = {
        "id": task.id,
        "event_id": task.event_id,
        "type": task.type,
        "label": task.label,
        "target": task.target,
        "target_value": task.target_value,
        "points": int(task.points or 0),
        "requires_confirmation": bool(task.requires_confirmation),
        "config": parse_task_config(task.config),
        "difficulty": getattr(task, "difficulty", None),
    }
    if task.type == "kc_target":
        # Precompute the NPC set (target + config.npcs) and its WOM metrics
        # once per state load — the matcher and the WOM reconciler both key
        # off these.
        d["kc_npcs"] = list(_kc_npcs(d))
        d["wom_metrics"] = _task_wom_metrics(task.type, d["kc_npcs"])
    if task.type == "item_collection":
        # Precompute the optional source-NPC restriction once per state load:
        # a per-item index (config.item_npcs) plus the single-item task-level
        # set (config.source_npcs). Empty => the item is unrestricted (any
        # source, incl. clog) — the feature is opt-in and a no-op by default.
        d["item_source_index"] = _item_source_index(d["config"])
        d["task_source_npcs"] = _norm_npc_set(d["config"].get("source_npcs"))
        # Names in the list flagged as pets (config.pet_items): credited from
        # `pet` submissions, never from drops/clogs.
        d["pet_name_set"] = _config_pet_names(d["config"])
        # any_path metric paths (KC / loot-value alternatives): normalized
        # once per state load, KC paths with their WOM boss metrics resolved.
        # The merged slug map rides in ``wom_metrics`` so the reconciler can
        # plan hiscores KC for these tasks exactly like kc_target's.
        d["metric_paths"] = list(_metric_paths_from_config(d["config"], resolve_wom=True))
        merged_wom: dict = {}
        for p in d["metric_paths"]:
            merged_wom.update(p.get("wom_metrics") or {})
        if merged_wom:
            d["wom_metrics"] = merged_wom
    if task.type == "loot_sweep":
        # Precompute the matcher's item->allowed-NPC index once per task (the
        # matcher stays pure/cheap; NPC scoping is v2). Guarded: the unit-test
        # conftest stubs services.loot_sweep.
        try:
            from services.loot_sweep import LootSweepConfig
            d["loot_sweep_index"] = LootSweepConfig(d["config"]).matcher_index()
        except Exception:
            d["loot_sweep_index"] = {}
    return d


def multi_clan_players(members_by_gid: dict, gids) -> set:
    """Player ids that belong to MORE THAN ONE of ``gids`` (G7). Each key of
    ``members_by_gid`` maps a group id to its member player-id set/iterable.
    Pure and O(total memberships) — the ambiguous-membership exclusion shared
    by the matcher (roster-less credit) and roster sync."""
    counts: dict = {}
    for gid in gids:
        # set() per clan: duplicate association rows (the NULL-user_id insert
        # race) must not read as membership in two clans.
        for pid in set(members_by_gid.get(gid, ())):
            counts[pid] = counts.get(pid, 0) + 1
    return {pid for pid, n in counts.items() if n > 1}


def load_matcher_state(session, now: Optional[datetime] = None) -> MatcherState:
    """Load all active events + tasks + bingo cells + rosters into a
    :class:`MatcherState`. One query burst per refresh, not per submission."""
    from db.models import Event, EventTask, EventTeam, EventTeamMember, EventBingoCell

    state = MatcherState(loaded_at=time.time())
    events = session.query(Event).filter(Event.status == "active").all()
    if not events:
        return state
    event_ids = [e.id for e in events]
    state.events = {e.id: _event_to_dict(e) for e in events}

    for task in session.query(EventTask).filter(EventTask.event_id.in_(event_ids)).all():
        if task.type not in AUTO_TASK_TYPES:
            continue
        state.tasks_by_event.setdefault(task.event_id, []).append(_task_to_dict(task))

    for cell in session.query(EventBingoCell).filter(
            EventBingoCell.event_id.in_(event_ids), EventBingoCell.task_id.isnot(None)).all():
        state.cells_by_task.setdefault(cell.task_id, []).append(
            {"id": cell.id, "idx": cell.idx, "event_id": cell.event_id,
             "label": cell.label})

    teams = session.query(EventTeam).filter(EventTeam.event_id.in_(event_ids)).all()
    state.team_names = {t.id: t.name for t in teams}
    team_event = {t.id: t.event_id for t in teams}
    if teams:
        members = session.query(EventTeamMember).filter(
            EventTeamMember.team_id.in_(list(team_event.keys()))).all()
        for m in members:
            state.participants.setdefault(m.player_id, []).append(
                (team_event[m.team_id], m.team_id, m.joined_at))

    # Whole-clan fallback teams (clan_vs_clan, no explicit roster): credit every
    # current member of the represented clan, so it runs "anyone in clan A vs
    # anyone in clan B". Recomputed each reload, so members who join the clan
    # mid-event start counting automatically. joined_at is None — the event
    # window is the only credit cutoff. One team per (event, player). G7: a
    # player who belongs to MORE THAN ONE of an event's participating clans is
    # ambiguous (which side are they on?) — they are excluded from every auto
    # team for that event and must be placed on a team explicitly by an admin.
    auto_teams = [t for t in teams if getattr(t, "auto_clan", False) and t.group_id]
    if auto_teams:
        from db.models.associations import user_group_association

        gids = list({t.group_id for t in auto_teams})
        rows = (
            session.query(
                user_group_association.c.group_id,
                user_group_association.c.player_id,
            )
            .filter(
                user_group_association.c.group_id.in_(gids),
                user_group_association.c.player_id.isnot(None),
            )
            .all()
        )
        # Sets, not lists: the known NULL-user_id insert race can duplicate
        # association rows, and a doubled row must not read as "two clans".
        members_by_gid: dict = {}
        for gid, pid in rows:
            members_by_gid.setdefault(gid, set()).add(pid)
        # Per-event multi-clan exclusions — computed per event because the same
        # player may legitimately anchor different clans across different events.
        gids_by_event: dict = {}
        for t in auto_teams:
            gids_by_event.setdefault(team_event[t.id], set()).add(t.group_id)
        multi_clan_by_event = {
            ev_id: multi_clan_players(members_by_gid, ev_gids)
            for ev_id, ev_gids in gids_by_event.items()
        }
        for t in auto_teams:
            event_id = team_event[t.id]
            excluded = multi_clan_by_event.get(event_id, ())
            for pid in members_by_gid.get(t.group_id, ()):  # noqa: E501
                if pid in excluded:
                    continue  # multi-clan member: never auto-credited (G7)
                existing = state.participants.setdefault(pid, [])
                if any(e == event_id for (e, _tid, _j) in existing):
                    continue  # already mapped to a team for this event
                existing.append((event_id, t.id, None))

    # Board-game turn pointers (web44a): only a team's CURRENT instance task
    # may match. Loaded last so a roll mid-refresh still lands next tick.
    board_ids = [eid for eid, e in state.events.items()
                 if e.get("kind") == "board_game"]
    if board_ids:
        from db.models import EventBoardPosition

        for pos in (session.query(EventBoardPosition)
                    .filter(EventBoardPosition.event_id.in_(board_ids)).all()):
            if pos.status == "active" and pos.current_task_id:
                state.board_task_by_team[(pos.event_id, pos.team_id)] = pos.current_task_id
    return state


def set_active_events(redis_conn, event_ids) -> None:
    """Maintain the ``events:active`` gate set (worker lifecycle duty)."""
    try:
        pipe = redis_conn.pipeline()
        pipe.delete(ACTIVE_EVENTS_KEY)
        ids = [int(i) for i in (event_ids or [])]
        if ids:
            pipe.sadd(ACTIVE_EVENTS_KEY, *ids)
            # Fail-safe TTL (P1-6): refreshed on every set; a dead consumer
            # lets it lapse so producers stop enqueuing.
            pipe.expire(ACTIVE_EVENTS_KEY, ACTIVE_EVENTS_TTL_SECONDS)
        pipe.execute()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# 3. Apply layer (worker + Task 18 confirmation flow)
# ══════════════════════════════════════════════════════════════════════════════

class StagedWrites:
    """Redis writes computed during matching but executed only after the DB
    transaction commits (P0-6).

    The XP-baseline / KC-watermark folds are the dedupe for cumulative
    counters: advancing them BEFORE the ledger row commits means a failed
    commit (deadlock, timeout, crash) rolls the row back while the watermark
    stays advanced — the replay then folds to delta 0 and the credit is gone
    forever. Passing a collector defers those writes to after the commit;
    passing ``staged=None`` keeps the historical write-immediately behaviour
    (tests, ad-hoc callers). Single-consumer assumption: envelopes for one
    (event, player) are applied sequentially, so a deferred watermark is
    always flushed before the next envelope that reads it.
    """

    __slots__ = ("ops",)

    def __init__(self):
        self.ops = []

    def stage(self, method: str, *args, **kwargs) -> None:
        self.ops.append((method, args, kwargs))

    def flush(self, redis_conn) -> None:
        """Execute the staged writes (call ONLY after a successful commit).
        A failed write here risks a one-off double-count on the next fold —
        rare and bounded, vs. the guaranteed lost credit of the old order —
        so it is logged loudly rather than raised."""
        ops, self.ops = self.ops, []
        for method, args, kwargs in ops:
            try:
                getattr(redis_conn, method)(*args, **kwargs)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "post-commit Redis write %s%r failed (watermark may lag "
                    "one submission)", method, args)


def _xp_baseline_key(event_id: int, player_id: int, skill: str) -> str:
    return f"events:{event_id}:xpbase:{player_id}:{_norm(skill)}"


def _fold_xp_baseline(redis_conn, event_id: int, player_id: int, skill, xp,
                      seed=None, staged: Optional[StagedWrites] = None) -> int:
    """Return xp gained since the last stored baseline and advance it.

    The first report after join only sets the baseline (delta 0) — PRD D10:
    no retroactive credit. Exception: WOM reconciler envelopes may carry a
    ``seed`` (the player's XP at the event window start, per WOM's snapshot
    history) — when the baseline is unset, the first fold credits from the
    seed instead, so plugin-less players still earn their in-window gains.

    With ``staged`` the baseline advance is deferred to post-commit (P0-6).
    """

    def _write(method, *args, **kwargs):
        if staged is not None:
            staged.stage(method, *args, **kwargs)
        else:
            getattr(redis_conn, method)(*args, **kwargs)

    try:
        xp = int(xp or 0)
    except (TypeError, ValueError):
        return 0
    if xp <= 0 or not skill:
        return 0
    key = _xp_baseline_key(event_id, player_id, skill)
    try:
        prev = redis_conn.get(key)
        if prev is None:
            _write("set", key, xp, ex=_STATE_KEY_TTL)
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                return 0
            return xp - seed if 0 < seed < xp else 0
        prev = int(prev)
        if xp <= prev:
            return 0
        _write("set", key, xp, ex=_STATE_KEY_TTL)
        return xp - prev
    except Exception:
        return 0


def _seed_allowed(joined_at, window_start) -> bool:
    """WOM window-start seeding is only honest for players who joined at/before
    the window start; late joiners keep the lazy first-report baseline."""
    if joined_at is None or window_start is None:
        return True
    return joined_at <= window_start


def _kc_fallback_key(event_id: int, task_id, player_id: int) -> str:
    # ``task_id`` is the bare task id, or a per-NPC scope ("id:npc") for
    # multi-NPC kc tasks (see _kc_state_scope).
    return f"events:{event_id}:kcfallback:{task_id}:{player_id}"


def _legacy_kcdedupe_max(redis_conn, event_id: int, task_id, player_id: int) -> int:
    """Max kill_count already credited via the pre-watermark ``kcdedupe`` set
    (deploy transition: don't re-credit kills counted under the old scheme)."""
    try:
        members = redis_conn.smembers(
            f"events:{event_id}:kcdedupe:{task_id}:{player_id}")
    except Exception:
        return 0
    best = 0
    for member in members or ():
        try:
            raw = member.decode() if isinstance(member, bytes) else str(member)
            best = max(best, int(raw.rpartition(":")[2]))
        except (ValueError, AttributeError, UnicodeDecodeError):
            continue
    return best


def _fold_kc_watermark(redis_conn, event_id: int, task_id, player_id: int,
                       kc_abs, *, seed=None, first_credit_offset: int = 0,
                       staged: Optional[StagedWrites] = None) -> int:
    """Return kills gained since the stored absolute-KC watermark, advance it.

    One watermark per (event, task-scope, player) — ``task_id`` is the bare
    task id, or a per-NPC scope for multi-NPC kc tasks (_kc_state_scope) —
    advanced by BOTH sources of absolute KC — plugin drops' ``kill_count`` and
    WOM hiscores — so whichever is ahead wins and the other folds to 0 (the
    double-count guard).

    First observation: baseline = ``seed`` (WOM's window-start KC) when given,
    else ``kc_abs - first_credit_offset`` (offset 1 keeps a first plugin drop
    crediting +1, as before). Credits already granted through the
    no-kill_count cooldown fallback (``kcfallback`` counter) are subtracted
    from any positive delta so they never double-count.

    With ``staged`` the watermark advance + fallback consume are deferred to
    post-commit (P0-6).
    """

    def _write(method, *args, **kwargs):
        if staged is not None:
            staged.stage(method, *args, **kwargs)
        else:
            getattr(redis_conn, method)(*args, **kwargs)

    try:
        kc_abs = int(kc_abs)
    except (TypeError, ValueError):
        return 0
    if kc_abs <= 0:
        return 0
    key = f"events:{event_id}:kcbase:{task_id}:{player_id}"
    try:
        prev = redis_conn.get(key)
        if prev is None:
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                seed = None
            base = seed if seed is not None and 0 < seed <= kc_abs \
                else max(kc_abs - int(first_credit_offset), 0)
            base = max(base, _legacy_kcdedupe_max(
                redis_conn, event_id, task_id, player_id))
        else:
            base = int(prev)
        delta = max(0, kc_abs - base)
        _write("set", key, max(kc_abs, base), ex=_STATE_KEY_TTL)
        if delta > 0:
            fb_key = _kc_fallback_key(event_id, task_id, player_id)
            try:
                pending = int(redis_conn.get(fb_key) or 0)
            except (TypeError, ValueError):
                pending = 0
            if pending > 0:
                consumed = min(pending, delta)
                delta -= consumed
                if pending - consumed > 0:
                    _write("set", fb_key, pending - consumed, ex=_STATE_KEY_TTL)
                else:
                    _write("delete", fb_key)
        return delta
    except Exception:
        return 0


# Without a usable kill count, stacks from one kill are only distinguishable
# from a new kill by time: one kill's stacks arrive within a couple of
# seconds, while re-killing any multi-stack NPC takes far longer.
KC_FALLBACK_COOLDOWN_SECONDS = 10


def _kc_dedupe(redis_conn, event_id: int, task_id, player_id: int,
               envelope: dict, staged: Optional[StagedWrites] = None) -> bool:
    """True if this drop represents a not-yet-counted kill for a kc task.

    ``task_id`` may carry a per-NPC scope for multi-NPC kc tasks (so the
    no-kill_count cooldown of one NPC never swallows a near-simultaneous kill
    of another, e.g. two Dagannoth Kings inside 10s).
    Keyed per (npc, kill_count) so multi-item drops from one kill count once.
    When kill_count is unusable (absent, or 0 = the plugin's "unavailable"
    marker), fall back to a per-(task, player) cooldown — the old guid
    fallback counted every stack of a multi-item kill as its own kill.

    With ``staged`` the marker writes are deferred to post-commit (P0-6): a
    rolled-back apply must not leave the kill marked as already counted.
    """
    data = envelope.get("data") or {}
    try:
        kill_count = int(data.get("kill_count"))
    except (TypeError, ValueError):
        kill_count = None
    key = f"events:{event_id}:kcdedupe:{task_id}:{player_id}"
    try:
        if kill_count is not None and kill_count > 0:
            member = f"{_norm(data.get('npc_name'))}:{kill_count}"
            if staged is not None:
                if redis_conn.sismember(key, member):
                    return False
                staged.stage("sadd", key, member)
                staged.stage("expire", key, _STATE_KEY_TTL)
                return True
            added = redis_conn.sadd(key, member)
            redis_conn.expire(key, _STATE_KEY_TTL)
            return bool(added)
        ts = int(envelope.get("ts") or time.time())
        last_key = f"events:{event_id}:kclast:{task_id}:{player_id}"
        last = redis_conn.get(last_key)
        if last is not None and ts - int(last) < KC_FALLBACK_COOLDOWN_SECONDS:
            return False
        if staged is not None:
            staged.stage("set", last_key, ts, ex=_STATE_KEY_TTL)
        else:
            redis_conn.set(last_key, ts, ex=_STATE_KEY_TTL)
        return True
    except Exception:
        # If Redis dedupe is unavailable, fall through — the ledger's unique
        # (task, team, guid) index still blocks exact replays.
        return True


def _publish(event_id: int, data: dict) -> None:
    try:
        from services.realtime import publish_event_update
        publish_event_update(event_id, data)
    except Exception:
        pass
    # Team-channel board posts (web54a): any event frame means the board may
    # look different — flag the event so the bot's refresher re-checks its
    # team posts (one cheap SADD; the refresher's per-team state hashes do
    # the precise "did MY view change?" filtering).
    try:
        from services.event_team_discord import TEAM_BOARD_DIRTY_KEY
        from utils.redis import redis_client

        # .client: the raw redis handle — the wrapper exposes no set ops.
        redis_client.client.sadd(TEAM_BOARD_DIRTY_KEY, str(event_id))
    except Exception:
        pass


def _enqueue_notification(session, notification_type: str, event: dict,
                          player_id: int, data: dict) -> None:
    """Insert a notification_queue row (create_notification pattern —
    data/submissions/common.py). Sending is Task 19; we only enqueue."""
    # In-game plugin inbox fan-out (docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md):
    # deliberately ahead of both the player_id guard (event-wide types carry a
    # representative player; the in-game audience is resolved from rosters)
    # and the Discord mute gate below — a player's in-game notifications are
    # independent of the event's Discord verbosity config. Best-effort.
    # event_task_progress is excluded: its Discord enqueue is itself gated on
    # message_config, so _maybe_enqueue_progress fans it out directly on every
    # increment instead.
    if notification_type != "event_task_progress":
        try:
            from services.plugin_notifications import fan_out_event_notification
            fan_out_event_notification(session, notification_type, event, data)
        except ImportError:
            pass  # unit-test stubs
        except Exception as plugin_notify_err:
            print(f"plugin inbox fan-out failed for {notification_type}: {plugin_notify_err}")
    if player_id is None:
        # Manual awards carry no player and notification_queue.player_id is
        # NOT NULL — those announce via the admin action itself (Task 19).
        return
    # Verbosity gate (web_events.message_config): a type the group leader
    # switched off is never even queued. The sender re-checks at send time
    # (covers rows queued before a config change), but this is the primary
    # gate — no queue churn for muted types.
    from services.event_notifications import (
        effective_message_config,
        should_send_event_message,
    )

    if not should_send_event_message(
        effective_message_config(event.get("message_config")), notification_type
    ):
        # Muted at the event level — but a per-team Discord channel (web53a)
        # may still want it. Coarse interest check here (any live row for the
        # team); the sender does the precise per-destination toggle filtering.
        try:
            from services.event_team_discord import team_channel_interest
        except ImportError:  # unit-test stubs
            return
        if not team_channel_interest(
            session, event["id"], notification_type, data.get("team_id")
        ):
            return
    from db.models.notification_queue import NotificationQueue
    payload = dict(data)
    payload["event_id"] = event["id"]
    payload.setdefault("event_name", event.get("name"))
    try:
        # notification_queue has a unique (type, player, group, data) index;
        # a re-completion after a revoke can build a byte-identical payload —
        # treat that as an already-queued notification, not an error.
        with session.begin_nested():
            session.add(NotificationQueue(
                notification_type=notification_type,
                player_id=player_id,
                data=json.dumps(payload, default=str),
                group_id=event.get("group_id"),
                status="pending",
            ))
            session.flush()
    except IntegrityError:
        pass


APPLIED_BONUS_STATUSES = ("auto", "confirmed", "manual")


def _team_completed_idxs(session, cells, team_id: int,
                         for_update: bool = False) -> set:
    """Board idxs the team has completed, given the event's cell rows.

    ``for_update`` makes it a locking (current) read — required when the
    caller holds the team-row mutex and must see rows committed by the
    transaction it just waited on, not its own pre-lock snapshot."""
    from db.models import EventBingoCompletion

    if not cells:
        return set()
    q = session.query(EventBingoCompletion).filter(
        EventBingoCompletion.cell_id.in_([c.id for c in cells]),
        EventBingoCompletion.team_id == team_id)
    if for_update:
        q = q.with_for_update()
    done_cell_ids = {row.cell_id for row in q.all()}
    return {c.idx for c in cells if c.id in done_cell_ids}


def _board_size_for(event: dict, cells) -> int:
    """Effective board size; 0 when the cell count isn't a matching square."""
    size = int(event.get("board_size") or 0)
    if size * size != len(cells):
        size = int(round(len(cells) ** 0.5)) if cells else 0
        if size * size != len(cells):
            return 0
    return size


def evaluate_bingo_bonuses(session, event: dict, team_id: int,
                           trigger_task_id: Optional[int] = None,
                           player_id: Optional[int] = None,
                           player_name: Optional[str] = None) -> list:
    """Award line/blackout bonuses newly earned by ``team_id`` (Task 20).

    Called after each newly completed bingo cell (and after free-cell grants).
    Idempotent with no separate state: the already-awarded set is derived from
    applied ``source_type='bonus'`` ledger rows whose deterministic ``note``
    names the line (``line:r3`` / ``line:c1`` / ``line:d0`` / ``blackout``).
    Each bonus row stores the points it granted in ``quantity`` so an unwind
    subtracts exactly what was added even if the event config changed later.
    Publishes ``kind: line|blackout`` SSE frames and enqueues ``event_line`` /
    ``event_blackout`` notifications. Returns [{note, points}, ...].
    """
    if not event.get("has_bingo") or team_id is None:
        return []
    line_pts = int(event.get("bonus_line_points") or 0)
    blackout_pts = int(event.get("bonus_blackout_points") or 0)
    if line_pts <= 0 and blackout_pts <= 0:
        return []

    from db.models import EventBingoCell, EventCompletion, EventTeam

    # Concurrency guard (batch 2): applies run in parallel lanes now, and two
    # teammates completing different cells can jointly finish the same line.
    # The team row is the per-team mutex — take it BEFORE deriving earned /
    # awarded, and read both via locking (current) reads, so a transaction
    # that waited here sees the winner's committed rows instead of its own
    # pre-lock snapshot (which is exactly the double-award).
    team = (session.query(EventTeam).filter(EventTeam.id == team_id)
            .with_for_update().first())  # doubles as the P0-7 score lock

    cells = session.query(EventBingoCell).filter(
        EventBingoCell.event_id == event["id"]).all()
    size = _board_size_for(event, cells)
    if not size:
        return []
    done_idxs = _team_completed_idxs(session, cells, team_id, for_update=True)

    earned = set()
    if line_pts > 0:
        earned.update(f"line:{key}" for key in completed_lines(size, done_idxs))
    if blackout_pts > 0 and len(done_idxs) == len(cells):
        earned.add(BLACKOUT_NOTE)
    if not earned:
        return []

    awarded = {
        row.note
        for row in session.query(EventCompletion).filter(
            EventCompletion.event_id == event["id"],
            EventCompletion.team_id == team_id,
            EventCompletion.source_type == "bonus",
            EventCompletion.status.in_(APPLIED_BONUS_STATUSES))
        .with_for_update().all()
        if row.note
    }
    new_notes = sorted(earned - awarded)
    if not new_notes:
        return []

    # Ledger rows need a task_id (NOT NULL): the triggering task, else any
    # task bound to a board cell, else any task on the event. An all-free
    # board on a task-less event has nowhere to hang a ledger row — skip.
    ledger_task_id = trigger_task_id
    if ledger_task_id is None:
        ledger_task_id = next((c.task_id for c in cells if c.task_id is not None), None)
    if ledger_task_id is None:
        from db.models import EventTask
        row = session.query(EventTask.id).filter(
            EventTask.event_id == event["id"]).first()
        ledger_task_id = row[0] if row else None
    if ledger_task_id is None:
        return []

    # ``team`` was locked at the top of the function (the per-team mutex).
    team_name = team.name if team is not None else None
    results = []
    line_notes = []
    for note in new_notes:
        points = blackout_pts if note == BLACKOUT_NOTE else line_pts
        session.add(EventCompletion(
            event_id=event["id"], task_id=ledger_task_id, team_id=team_id,
            player_id=player_id, status="auto", quantity=points,
            source_type="bonus", note=note))
        if team is not None:
            team.score = int(team.score or 0) + points
        kind = "blackout" if note == BLACKOUT_NOTE else "line"
        frame = {"kind": kind, "event_id": event["id"], "team_id": team_id,
                 "bonus_points": points, "note": note}
        if team is not None:
            frame["team_score"] = int(team.score or 0)
        if player_name:
            frame["player_name"] = player_name
        _publish(event["id"], frame)
        if note == BLACKOUT_NOTE:
            _enqueue_notification(session, "event_blackout", event, player_id, {
                "team_id": team_id,
                "team_name": team_name,
                "bonus_points": points,
                "line": note,
                "team_score": int(team.score or 0) if team is not None else None,
            })
        else:
            line_notes.append(note)
        results.append({"note": note, "points": points})
    # One cell can finish several lines at once (row + column + diagonal);
    # per-line notifications rendered as indistinguishable "completed a full
    # line" triplets, so all lines earned in one evaluation share a single
    # message that names them (renderers turn the notes into labels).
    if line_notes:
        _enqueue_notification(session, "event_line", event, player_id, {
            "team_id": team_id,
            "team_name": team_name,
            "bonus_points": line_pts * len(line_notes),
            "line": line_notes[0],
            "lines": line_notes,
            "team_score": int(team.score or 0) if team is not None else None,
        })
    session.flush()
    return results


def grant_free_cells(session, event) -> int:
    """Complete every free cell (``task_id`` NULL) for every team of ``event``.

    Free cells count as completed for all teams "from activation": Task 21's
    explicit activation action calls this; until then the board PUT calls it
    when the board is (re)saved while the event is already live (today's
    implicit lifecycle — create_event activates immediately). Idempotent per
    (cell, team); evaluates bonuses for each touched team. ``event`` may be
    the ORM row or an engine event dict. Returns the number of completions
    inserted. Caller owns the commit.
    """
    from db.models import EventBingoCell, EventBingoCompletion, EventTeam

    ev = event if isinstance(event, dict) else _event_to_dict(event)
    if not ev.get("has_bingo"):
        return 0
    free_cells = session.query(EventBingoCell).filter(
        EventBingoCell.event_id == ev["id"],
        EventBingoCell.task_id.is_(None)).all()
    if not free_cells:
        return 0
    teams = session.query(EventTeam).filter(EventTeam.event_id == ev["id"]).all()
    if not teams:
        return 0
    existing = {
        (row.cell_id, row.team_id)
        for row in session.query(EventBingoCompletion).filter(
            EventBingoCompletion.cell_id.in_([c.id for c in free_cells])).all()
    }
    inserted = 0
    touched_teams = set()
    for team in teams:
        for cell in free_cells:
            if (cell.id, team.id) in existing:
                continue
            session.add(EventBingoCompletion(
                cell_id=cell.id, team_id=team.id, player_id=None))
            inserted += 1
            touched_teams.add(team.id)
            _publish(ev["id"], {
                "kind": "cell", "event_id": ev["id"], "task_id": None,
                "team_id": team.id, "cell_idx": cell.idx,
                "cell_label": cell.label, "free": True,
            })
    if inserted:
        session.flush()
        for team_id in sorted(touched_teams):
            evaluate_bingo_bonuses(session, ev, team_id)
    return inserted


def reconcile_bingo_bonuses(session, event) -> dict:
    """Re-derive every team's bonus set from the current board state.

    Used after a *live* board replace (implicit lifecycle lets a never-
    scheduled active event edit its board): unwinds bonuses for lines the new
    board no longer holds and awards ones it already satisfies (e.g. all-free
    lines). Idempotent. ``event`` is the ORM row or an engine event dict.
    Returns {team_id: {"revoked": [...], "awarded": [...]}} for teams that
    changed.
    """
    from db.models import EventTeam

    ev = event if isinstance(event, dict) else _event_to_dict(event)
    summary: dict = {}
    if not ev.get("has_bingo"):
        return summary
    for team in session.query(EventTeam).filter(EventTeam.event_id == ev["id"]).all():
        revoked = _unwind_bonuses(session, ev, team.id)
        awarded = [a["note"] for a in evaluate_bingo_bonuses(session, ev, team.id)]
        if revoked or awarded:
            summary[team.id] = {"revoked": revoked, "awarded": awarded}
    if summary:
        session.flush()
    return summary


def _complete_bingo_cells(session, event: dict, task: dict, team_id: int,
                          player_id: int, cells: list,
                          player_name: Optional[str] = None) -> list:
    """Insert web_event_bingo_completions once per (cell, team) and evaluate
    line/blackout bonuses. Returns the cell dicts that were newly completed.

    A cell only ever completes in lockstep with its task (this is only
    called from the ``newly_completed`` branch of :func:`apply_ledger_row`),
    so a standalone ``event_cell`` notification would always be a duplicate
    of the ``event_completion`` message the caller enqueues right after —
    the caller folds the newly-completed cells' labels into that one message
    instead of us enqueuing a second one here.
    """
    if not cells or not event.get("has_bingo"):
        return []
    from db.models import EventBingoCompletion

    newly = []
    cell_ids = [c["id"] for c in cells]
    existing = {
        row.cell_id
        for row in session.query(EventBingoCompletion).filter(
            EventBingoCompletion.cell_id.in_(cell_ids),
            EventBingoCompletion.team_id == team_id).all()
    }
    for cell in cells:
        if cell["id"] in existing:
            continue
        session.add(EventBingoCompletion(
            cell_id=cell["id"], team_id=team_id, player_id=player_id))
        newly.append(cell)
    if newly:
        evaluate_bingo_bonuses(session, event, team_id,
                               trigger_task_id=task["id"],
                               player_id=player_id, player_name=player_name)
    return newly


def _task_contributors(session, task_id, team_id) -> list:
    """Per-player net quantity contributed to one (task, team), from the
    applied ledger (``auto``/``confirmed``/``manual``, bonus rows excluded).
    Powers the ``event_completion`` notification's "who completed it" list —
    largest contribution first, ties broken by player id for stability."""
    from db.models import EventCompletion, Player

    rows = (
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task_id,
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(APPLIED_BONUS_STATUSES))
        .all()
    )
    totals: dict = {}
    for r in rows:
        if (r.source_type or "") == "bonus" or r.player_id is None:
            continue
        totals[r.player_id] = totals.get(r.player_id, 0) + max(int(r.quantity or 1), 1)
    if not totals:
        return []
    names = {
        p.player_id: p.player_name
        for p in session.query(Player).filter(Player.player_id.in_(totals.keys())).all()
    }
    contributors = [
        {"player_id": pid, "player_name": names.get(pid) or f"Player {pid}", "quantity": qty}
        for pid, qty in totals.items()
    ]
    contributors.sort(key=lambda c: (-c["quantity"], c["player_id"]))
    return contributors


def _award_contribution_points(session, event: dict, task: dict, team_id,
                               contributors: list, points: int) -> None:
    """Split a completed task's points across its contributors by net share
    (``points × quantity / total_quantity``, floats) and persist one
    ``EventPlayerPoints`` row per player. Idempotent rewrite: existing rows
    for the (task, team) are replaced, so re-application after a revoke
    redistributes cleanly. Mutates each contributor dict with its
    ``points_share`` so the completion notification can show it."""
    from db.models import EventPlayerPoints

    (session.query(EventPlayerPoints)
     .filter(EventPlayerPoints.task_id == task["id"],
             EventPlayerPoints.team_id == team_id)
     .delete(synchronize_session=False))
    if not points or not contributors:
        return
    total = sum(max(int(c.get("quantity") or 0), 0) for c in contributors)
    if total <= 0:
        return
    for c in contributors:
        share = round(points * max(int(c.get("quantity") or 0), 0) / total, 2)
        if share <= 0:
            continue
        c["points_share"] = share
        session.add(EventPlayerPoints(
            event_id=event["id"], task_id=task["id"], team_id=team_id,
            player_id=c["player_id"], points=share))


def _maybe_enqueue_progress(session, event: dict, task: dict, team_id, player_id,
                            player_name, previous: int, current: int,
                            proof_url: Optional[str] = None,
                            matched_target: Optional[str] = None,
                            threshold: Optional[int] = None) -> None:
    """Enqueue an ``event_task_progress`` notification when the event's
    message_config asks for one ('all': every increment; 'milestones': only
    when a 25/50/75% threshold was crossed). Completion itself is announced
    by ``event_completion`` — this never fires for the crossing into 100%.

    ``proof_url`` is the screenshot (if any) attached to the ledger row that
    drove this increment, carried through so the sender can attach it to the
    Discord message the same way a completion's proof does.

    The in-game plugin inbox is fanned out here on every increment, before
    the Discord verbosity gates — the plugin has its own client-side progress
    toggle, and the HUD advances off these envelopes between state refreshes.
    Exception: continuous-metric tasks (xp_target/loot_value), where every
    XP drop or loot stack is an increment — those only fan out when a
    {PLUGIN_PROGRESS_STEP_PCT}% step of the target was crossed, so a 10M-XP
    task pings its teams ~10 times total instead of per kill.
    """
    from services.event_notifications import (
        effective_message_config,
        progress_milestones_crossed,
    )

    if current <= previous:
        return

    # Callers with team context pass the effective threshold (whole_team pb
    # tasks scale to the roster); the pure fallback covers the rest.
    target_threshold = threshold if threshold is not None else completion_threshold(task)
    base_payload = {
        "task_id": task["id"],
        "task_label": task.get("label"),
        "team_id": team_id,
        "player_id": player_id,
        "player_name": player_name,
        "progress": current,
        "target": target_threshold,
        "proof_url": proof_url,
    }
    crossed_pcts = progress_milestones_crossed(previous, current, target_threshold)
    if crossed_pcts:
        base_payload["milestone_pct"] = max(crossed_pcts)
    send_plugin = True
    if task.get("type") in PLUGIN_PROGRESS_STEP_TASK_TYPES:
        send_plugin = _plugin_progress_step_crossed(previous, current, target_threshold)
    if send_plugin:
        try:
            from services.plugin_notifications import (
                fan_out_event_notification,
                resolve_item_icon_id,
            )
            plugin_payload = dict(base_payload)
            if matched_target:
                # Name + sprite of the drop behind this increment, so the
                # client can say "received X, progressing Y" with an icon.
                plugin_payload["received_item"] = matched_target
                icon_item_id = resolve_item_icon_id(session, matched_target)
                if icon_item_id:
                    plugin_payload["icon_item_id"] = icon_item_id
            fan_out_event_notification(session, "event_task_progress", event, plugin_payload)
        except ImportError:
            pass  # unit-test stubs
        except Exception as plugin_notify_err:
            print(f"plugin progress fan-out failed: {plugin_notify_err}")

    config = effective_message_config(event.get("message_config"))
    mode = config.get("task_progress", "off")
    if not config["toggles"].get("event_task_progress", True):
        mode = "off"
    # Per-team Discord channels (web53a) carry their own progress verbosity
    # (default 'all') — enqueue at the most verbose mode ANY audience wants;
    # the sender filters per destination (a 'milestones' main channel skips
    # rows without milestone_pct).
    team_mode = "off"
    if team_id is not None:
        try:
            from services.event_team_discord import team_progress_interest

            team_mode = team_progress_interest(session, event["id"], team_id)
        except Exception:
            team_mode = "off"
    _ORDER = {"off": 0, "milestones": 1, "all": 2}
    effective = mode if _ORDER[mode] >= _ORDER[team_mode] else team_mode
    if effective == "off":
        return
    if effective == "milestones" and not crossed_pcts:
        return
    _enqueue_notification(session, "event_task_progress", event, player_id, base_payload)


def _current_leader(session, event_id: int, strict: bool = False):
    """(team_id, score) of the current points leader, or None.

    ``strict=True`` returns None on a shared top score — a team that merely
    TIES the leader has not taken the lead (the id-order tiebreak used to
    crown them and fire a bogus lead-change embed)."""
    from db.models import EventTeam
    rows = (session.query(EventTeam)
            .filter(EventTeam.event_id == event_id)
            .order_by(EventTeam.score.desc(), EventTeam.id.asc())
            .limit(2).all())
    if not rows:
        return None
    # round(), not int(): loot_sweep scores carry 2dp decimals — truncation
    # made a 100.8 vs 100.2 overtake read as a tie (no lead-change fires) and
    # showed truncated scores in the embeds (audit).
    if strict and len(rows) > 1 and (
            round(float(rows[0].score or 0), 2)
            == round(float(rows[1].score or 0), 2)):
        return None
    return (rows[0].id, round(float(rows[0].score or 0), 2))


def _loot_sweep_applied_rows(session, task: dict, team_id) -> list:
    """Applied ledger rows for one loot_sweep (task, team) — the recompute
    input for its running score (same status set as the item-list rollups)."""
    from db.models import EventCompletion

    return list(
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(APPLIED_BONUS_STATUSES))
        .all()
    )


def _loot_sweep_score(session, task: dict, team_id, *, include=None, exclude_id=None) -> dict:
    """Full loot_sweep breakdown for a (task, team), optionally folding an
    unsaved ``include`` row and/or dropping a soon-to-be-revoked ``exclude_id``
    (so callers can score the before/after of a single change)."""
    from services.loot_sweep import LootSweepConfig, score_rows

    rows = _loot_sweep_applied_rows(session, task, team_id)
    if exclude_id is not None:
        rows = [r for r in rows if r.id != exclude_id]
    if include is not None and all(r.id != include.id for r in rows):
        rows = rows + [include]
    return score_rows(rows, LootSweepConfig(task.get("config") or {}))


def _loot_sweep_rank(session, event_id: int, team_id) -> tuple:
    """``(rank, team_count)`` for one team in an event's standings — rank is
    1-based by score (desc, id tiebreak), ``None`` when the team isn't found.
    Used to enrich loot_sweep completion messages with "#2/4 teams"."""
    from db.models import EventTeam

    rows = (session.query(EventTeam.id, EventTeam.score)
            .filter(EventTeam.event_id == event_id)
            .order_by(EventTeam.score.desc(), EventTeam.id.asc())
            .all())
    rank = None
    for i, (tid, _score) in enumerate(rows):
        if tid == team_id:
            rank = i + 1
            break
    return rank, len(rows)


def _apply_loot_sweep(session, redis_conn, event: dict, task: dict, completion,
                      player_name: Optional[str] = None) -> dict:
    """Apply one loot_sweep receipt: recompute the team's running total for the
    task off the ledger, fold the delta into the team score, and emit side
    effects. Loot_sweep tasks never "complete" — ``EventProgress.progress`` is
    the running point total (not a threshold count) and ``completed`` stays
    False, so more receipts keep scoring until the per-item caps / set-bonus
    cap stop advancing it (enforced in :func:`_row_advances_progress`)."""
    from db.models import EventProgress, EventTeam
    from services.loot_sweep import LootSweepConfig, receipt_detail

    team_id = completion.team_id
    player_id = completion.player_id
    config = LootSweepConfig(task.get("config") or {})

    # P0-7: locked read — progress is a read-modify-write shared between the
    # consumer and webapi confirm/revoke flows; unlocked, one side's update
    # silently overwrites the other's under REPEATABLE READ.
    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
                .with_for_update()
                .first())
    if progress is None:
        progress = EventProgress(
            event_id=event["id"], task_id=task["id"], team_id=team_id,
            progress=0, completed=False)
        session.add(progress)
    # Loot Sweep points are decimal-valued (2dp — a 1-pointer's second
    # receipt at 20% decay is 0.8), so totals/deltas round at 2dp throughout.
    previous_total = round(float(progress.progress or 0), 2)

    # ``previous`` excludes just this row; ``current`` folds it in. Both are
    # scored from the ledger so the delta (and any set-completion crossing) is
    # exact and revoke-symmetric.
    prev = _loot_sweep_score(session, task, team_id, exclude_id=completion.id)
    curr = _loot_sweep_score(session, task, team_id, include=completion)
    new_total = curr["total"]
    delta = round(new_total - previous_total, 2)
    progress.progress = new_total
    progress.completed = False

    team_score = None
    lead_changed_to = None
    if delta and team_id is not None:
        previous_leader = _current_leader(session, event["id"], strict=True)
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = round(float(team.score or 0) + delta, 2)
            team_score = team.score
            session.flush()
            new_leader = _current_leader(session, event["id"], strict=True)
            if (new_leader is not None and new_leader[0] == team_id
                    and (previous_leader is None or previous_leader[0] != team_id)):
                lead_changed_to = new_leader

    # Contribution split over the whole running total (by receipt share), so
    # end-of-event player points track who fed the sweep — rewritten each time.
    contributors = _task_contributors(session, task["id"], team_id)
    _award_contribution_points(session, event, task, team_id, contributors, new_total)

    # A "milestone" = a group or the whole set just completed (a bonus was
    # earned). Groups/sets are the announce-worthy moments; the per-receipt
    # ``event_sweep_item`` message (opt-in) covers everything else.
    set_completed = curr["set_awarded"] > prev["set_awarded"]
    set_bonus_delta = round(curr["set_total"] - prev["set_total"], 2)
    new_group_label = None
    new_group_bonus = 0.0
    new_group_n = 0
    for pg, cg in zip(prev["groups"], curr["groups"]):
        if cg["awarded"] > pg["awarded"]:
            new_group_label = cg["label"] or task.get("label")
            new_group_bonus = round(cg["bonus_total"] - pg["bonus_total"], 2)
            new_group_n = cg["awarded"]
            break
    bonus_delta = ((curr["group_bonus_total"] + curr["set_total"])
                   - (prev["group_bonus_total"] + prev["set_total"]))
    result = {
        "kind": "loot_sweep",
        "event_id": event["id"],
        "task_id": task["id"],
        "team_id": team_id,
        "player_id": player_id,
        "points": delta,
        "total": new_total,
        "received_item": completion.matched_target,
        "set_completed": set_completed,
        "group_completed": new_group_label,
    }
    if team_score is not None:
        result["team_score"] = team_score
    session.flush()

    frame = dict(result)
    if player_name:
        frame["player_name"] = player_name
    frame["task_label"] = task.get("label")
    _publish(event["id"], frame)

    # In-game plugin inbox: "received X (+N)". Best-effort; unit-test stubs
    # lack the plugin module.
    try:
        from services.plugin_notifications import (
            fan_out_event_notification, resolve_item_icon_id,
        )
        plugin_payload = {
            "task_id": task["id"], "task_label": task.get("label"),
            "team_id": team_id, "player_id": player_id, "player_name": player_name,
            "points": delta, "team_score": team_score, "progress": new_total,
            "received_item": completion.matched_target,
        }
        if completion.matched_target:
            icon = resolve_item_icon_id(session, completion.matched_target)
            if icon:
                plugin_payload["icon_item_id"] = icon
        fan_out_event_notification(session, "event_task_progress", event, plugin_payload)
    except ImportError:
        pass
    except Exception as plugin_err:
        print(f"loot_sweep plugin fan-out failed: {plugin_err}")

    # Discord: three independently-toggleable loot_sweep verbosity levels
    # (services.event_notifications DEFAULT_MESSAGE_TOGGLES / message_config),
    # each enriched with the item/points detail that used to live only on the
    # website. Whole-set + subset completions default on; individual item
    # receipts default off (a game-wide sweep would flood the channel) and are
    # additionally gated by loot_sweep.item_min_points. A message is enqueued
    # when the event-level config wants it OR any per-team channel does (web53a
    # pattern) — the sender applies each destination's own toggle.
    if team_id is not None:
        from services.event_notifications import (
            effective_message_config, should_send_event_message,
        )

        cfg = effective_message_config(event.get("message_config"))

        def _sweep_wanted(ntype: str) -> bool:
            if should_send_event_message(cfg, ntype):
                return True
            try:
                from services.event_team_discord import team_channel_interest
                return team_channel_interest(session, event["id"], ntype, team_id)
            except Exception:
                return False

        detail = receipt_detail(curr, prev, config, completion.matched_target)
        team_rank, team_count = _loot_sweep_rank(session, event["id"], team_id)
        # Fields shared by every sweep message for this receipt.
        base = {
            "task_id": task["id"], "task_label": task.get("label"),
            "team_id": team_id, "player_id": player_id, "player_name": player_name,
            "team_score": team_score, "team_rank": team_rank, "team_count": team_count,
            "points_based": True, "loot_sweep": True,
            "received_item": completion.matched_target,
            "source_type": completion.source_type,
            "proof_url": completion.proof_url,
        }
        if detail:
            base["group_label"] = detail["group_label"]
            base["npcs"] = detail["npcs"]
            # Custom boss/category art for the group/set message thumbnail
            # (resolved by the sender; falls back to the received item's icon).
            if detail.get("group_image"):
                base["group_image"] = detail["group_image"]

        # 1) Individual scoring receipt (opt-in, min-points gated).
        min_pts = int((cfg.get("loot_sweep") or {}).get("item_min_points", 0) or 0)
        if (detail and detail["received_points"] > 0
                and detail["received_points"] >= min_pts
                and _sweep_wanted("event_sweep_item")):
            item_payload = dict(base)
            item_payload.update({
                "received_qty": max(int(getattr(completion, "quantity", 1) or 1), 1),
                "received_points": detail["received_points"],
                "npc": detail["npcs"][0] if detail["npcs"] else None,
                "item_scored": detail["item_scored"], "item_max": detail["item_max"],
                "item_remaining": detail["item_remaining"],
                "next_receipt_points": detail["next_receipt_points"],
                "group_have": detail["group_have"], "group_need": detail["group_need"],
                "progress": new_total,
            })
            _enqueue_notification(session, "event_sweep_item", event, player_id, item_payload)

        # 2) Subset (group) bonus just completed.
        if new_group_label and _sweep_wanted("event_sweep_group"):
            grp_payload = dict(base)
            grp_payload.update({
                "group_label": new_group_label,
                "bonus_points": new_group_bonus,
                "completion_n": new_group_n,
                "contributors": contributors,
            })
            _enqueue_notification(session, "event_sweep_group", event, player_id, grp_payload)

        # 3) Whole-set bonus just completed.
        if set_completed and _sweep_wanted("event_sweep_set"):
            set_payload = dict(base)
            set_payload.update({
                "bonus_points": set_bonus_delta,
                "completion_n": curr["set_awarded"],
                "set_completions": curr["set_completions"],
                "contributors": contributors,
            })
            _enqueue_notification(session, "event_sweep_set", event, player_id, set_payload)

    if lead_changed_to:
        # Enrich the lead-change with the receipt that triggered it (item +
        # points), so the message can name the drop that took the lead.
        _enqueue_notification(session, "event_lead_change", event, player_id, {
            "team_id": team_id,
            "team_score": lead_changed_to[1],
            "task_id": task["id"],
            "task_label": task.get("label"),
            "received_item": completion.matched_target,
            "points": delta,
        })
    return result


def _revoke_loot_sweep(session, event: dict, task: dict, team_id, completion) -> dict:
    """Recompute a loot_sweep (task, team) after a receipt was revoked. The
    caller already flipped the row to ``revoked``, so the applied-ledger score
    excludes it; we re-fold the running total and take back the delta."""
    from db.models import EventProgress, EventTeam

    # P0-7: locked read — progress is a read-modify-write shared between the
    # consumer and webapi confirm/revoke flows; unlocked, one side's update
    # silently overwrites the other's under REPEATABLE READ.
    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
                .with_for_update()
                .first())
    previous_total = round(float(progress.progress or 0), 2) if progress is not None else 0.0
    new_total = _loot_sweep_score(session, task, team_id)["total"]
    delta = round(new_total - previous_total, 2)

    if progress is None:
        if new_total <= 0:
            return {"progress": 0, "completed": False, "team_score": None}
        progress = EventProgress(event_id=event["id"], task_id=task["id"],
                                 team_id=team_id, progress=0, completed=False)
        session.add(progress)
    progress.progress = new_total
    progress.completed = False

    team_score = None
    if delta and team_id is not None:
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = round(float(team.score or 0) + delta, 2)
            team_score = team.score

    contributors = _task_contributors(session, task["id"], team_id)
    _award_contribution_points(session, event, task, team_id, contributors, new_total)

    session.flush()
    frame = {"kind": "revoke", "event_id": event["id"], "task_id": task["id"],
             "team_id": team_id, "progress": new_total, "loot_sweep": True}
    if team_score is not None:
        frame["team_score"] = team_score
    _publish(event["id"], frame)
    return {"progress": new_total, "completed": False, "team_score": team_score}


def apply_ledger_row(session, redis_conn, event: dict, task: dict, completion,
                     cells: Optional[list] = None,
                     player_name: Optional[str] = None) -> dict:
    """Fold one non-pending ledger row into progress / score / bingo state and
    emit SSE + notification side effects. Shared with Task 18: the confirm
    flow calls this (via :func:`apply_completion`) when a pending row is
    approved. Caller owns the transaction; this only flushes.
    """
    from db.models import EventProgress, EventTeam

    # Loot Sweep scores continuously off the ledger (decaying per-receipt
    # item points + set bonuses) instead of completing once — its own path.
    if _list_kind(task) == "loot_sweep":
        return _apply_loot_sweep(session, redis_conn, event, task, completion,
                                 player_name=player_name)

    team_id = completion.team_id
    player_id = completion.player_id
    quantity = int(completion.quantity or 1)

    # P0-7: locked read — progress is a read-modify-write shared between the
    # consumer and webapi confirm/revoke flows; unlocked, one side's update
    # silently overwrites the other's under REPEATABLE READ.
    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
                .with_for_update()
                .first())
    if progress is None:
        progress = EventProgress(
            event_id=event["id"], task_id=task["id"], team_id=team_id,
            progress=0, completed=False)
        session.add(progress)
    already_completed = bool(progress.completed)
    previous_progress = int(progress.progress or 0)
    if player_id is not None and not already_completed:
        # HUD focus (docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md): this player's
        # own submission just advanced this task — remember it as what they
        # are working toward. Best-effort Redis stamp.
        try:
            from services.plugin_notifications import stamp_player_focus
            stamp_player_focus(player_id, event["id"], task["id"])
        except ImportError:
            pass  # unit-test stubs
        except Exception:
            pass
    # whole_team pb thresholds depend on the team's roster — resolve once and
    # use it for the completion decision AND every frame/payload target.
    threshold = effective_threshold(session, task, team_id)
    if _pb_distinct_players(task):
        # Distinct-player semantics: a grinder re-beating the time is still
        # one player; recompute from the applied ledger like all_of items.
        progress.progress = _distinct_player_progress(
            session, task, team_id, threshold, include=completion)
    elif _list_kind(task) in ("all_of", "assembly"):
        # Distinct-item semantics: recompute from the applied ledger instead
        # of folding quantity (which let one big stack complete the set).
        progress.progress = _distinct_item_progress(session, task, team_id, include=completion)
    elif _list_kind(task) == "groups":
        progress.progress = _grouped_item_progress(session, task, team_id, include=completion)
    elif _list_kind(task) == "any_path":
        progress.progress = _anypath_item_progress(session, task, team_id, include=completion)
    else:
        progress.progress = int(progress.progress or 0) + quantity

    result = {
        "kind": "progress",
        "event_id": event["id"],
        "task_id": task["id"],
        "team_id": team_id,
        "player_id": player_id,
        "progress": progress.progress,
        "target": threshold,
    }

    newly_completed = (not already_completed
                       and progress.progress >= threshold)
    if not newly_completed:
        # Once completed, further ledger rows still record but don't
        # re-complete or re-score.
        session.flush()
        if not already_completed:
            frame = dict(result)
            if player_name:
                frame["player_name"] = player_name
            _publish(event["id"], frame)
            _maybe_enqueue_progress(
                session, event, task, team_id, player_id, player_name,
                previous_progress, int(progress.progress or 0),
                proof_url=completion.proof_url,
                matched_target=completion.matched_target,
                threshold=threshold)
        return result

    progress.completed = True
    progress.completed_at = datetime.now()
    result["kind"] = "completion"

    team_score = None
    points = int(task.get("points") or 0)
    lead_changed_to = None
    if points and team_id is not None:
        # Strict leaders only: a tie has no leader, so tying the top score
        # never announces a lead change, while later breaking that tie does.
        previous_leader = _current_leader(session, event["id"], strict=True)
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = int(team.score or 0) + points
            team_score = team.score
            session.flush()
            new_leader = _current_leader(session, event["id"], strict=True)
            if (new_leader is not None and new_leader[0] == team_id
                    and (previous_leader is None
                         or previous_leader[0] != team_id)):
                lead_changed_to = new_leader
    result["points"] = points
    if team_score is not None:
        result["team_score"] = team_score

    new_cells = _complete_bingo_cells(
        session, event, task, team_id, player_id, cells or [],
        player_name=player_name)
    session.flush()

    # Board-game turn side-effects (web44a): coins, awaiting_roll, and — in
    # auto-trigger mode — the dice roll itself. Same transaction; a board
    # failure must not lose the completion, hence the broad guard.
    board = None
    if event.get("kind") == "board_game" and team_id is not None:
        try:
            from services.boardgame_engine import handle_board_completion

            board = handle_board_completion(
                session, redis_conn, event, task, team_id, player_id)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "board-game completion side-effects failed")
    if board is not None:
        result["board"] = board
        if board.get("roll"):
            # Auto-trigger mode rolled immediately: announce the move + the
            # freshly drawn task alongside the completion message.
            roll = board["roll"]
            dice = roll.get("dice") or []
            _enqueue_notification(session, "event_board_turn", event, player_id, {
                "team_id": team_id,
                "player_name": player_name,
                "dice": dice,
                "dice_str": " + ".join(str(d) for d in dice) or "?",
                "tile_from": roll.get("from"),
                "tile_to": roll.get("to"),
                "turn": roll.get("turn"),
                "won": bool(roll.get("won")),
                "next_task_label": roll.get("task_label") or "—",
                "coins_awarded": board.get("coins_awarded") or 0,
                "coin_balance": board.get("coin_balance") or 0,
            })
        else:
            # Manual-trigger mode: the team is now awaiting_roll — nudge them
            # to roll (web53a). Main-channel default is OFF; this primarily
            # feeds the per-team Discord channels.
            _enqueue_notification(session, "event_board_roll_prompt", event, player_id, {
                "team_id": team_id,
                "player_name": player_name,
                "task_id": task["id"],
                "task_label": task.get("label"),
                "coins_awarded": board.get("coins_awarded") or 0,
                "coin_balance": board.get("coin_balance"),
            })

    frame = dict(result)
    if player_name:
        frame["player_name"] = player_name
    frame["task_label"] = task.get("label")
    _publish(event["id"], frame)
    for cell in new_cells:
        _publish(event["id"], {
            "kind": "cell", "event_id": event["id"], "task_id": task["id"],
            "team_id": team_id, "cell_idx": cell["idx"],
            "cell_label": cell.get("label"),
        })

    # Everyone who fed this task, not just whoever's submission tipped it
    # over the line — the completion message folds them all in, and the
    # task's points are split across them by net contribution share.
    contributors = _task_contributors(session, task["id"], team_id)
    _award_contribution_points(session, event, task, team_id, contributors, points)

    notification = {
        "task_id": task["id"],
        "task_label": task.get("label"),
        "team_id": team_id,
        "player_id": player_id,
        "player_name": player_name,
        "points": points,
        "team_score": team_score,
        "cell_idxs": [c["idx"] for c in new_cells],
        "cell_labels": [c.get("label") for c in new_cells if c.get("label")],
        "source_type": completion.source_type,
        "proof_url": completion.proof_url,
        "contributors": contributors,
    }
    if _list_kind(task) == "point_collection":
        # Ledger quantities on this task are point credits, not item counts —
        # renderers must not read them as "2× Bandos chestplate".
        notification["points_based"] = True
    # In-game rendering (plugin inbox): the finishing item's game id, so the
    # client can draw the sprite locally. Independent of the item_details
    # verbosity toggle below — an icon is not "verbose detail".
    if completion.matched_target:
        try:
            from services.plugin_notifications import resolve_item_icon_id
            icon_item_id = resolve_item_icon_id(session, completion.matched_target)
            if icon_item_id:
                notification["icon_item_id"] = icon_item_id
        except ImportError:
            pass  # unit-test stubs
        except Exception:
            pass
    # Verbose completion detail (item_details config, on by default): name the
    # item that finished the task, its drop quantity, how much of the
    # requirement that final drop filled (the progress delta), and the target.
    # Only item completions carry a matched_target; the sender/layout drop the
    # line when these are absent, so an off toggle simply omits them here.
    from services.event_notifications import effective_message_config

    if (completion.matched_target
            and effective_message_config(event.get("message_config")).get("item_details", True)):
        notification.update({
            "received_item": completion.matched_target,
            "received_qty": int(completion.quantity or 0),
            "contributed": max(int(progress.progress or 0) - int(previous_progress or 0), 0),
            "target": threshold,
        })
    # Bingo events summarize the team's board standing on completion (total
    # tiles done / team position) instead of naming the single tile marked.
    # Post-completion state: score + new_cells were already flushed above.
    if event.get("has_bingo") and team_id is not None:
        from db.models import EventBingoCompletion

        tiles_done = (session.query(EventBingoCompletion)
                      .filter(EventBingoCompletion.team_id == team_id).count())
        ranked = (session.query(EventTeam)
                  .filter(EventTeam.event_id == event["id"])
                  .order_by(EventTeam.score.desc(), EventTeam.id.asc()).all())
        rank = next((i + 1 for i, t in enumerate(ranked) if t.id == team_id), None)
        notification.update({
            "tiles_completed": tiles_done,
            "team_rank": rank,
            "team_count": len(ranked),
        })
    _enqueue_notification(session, "event_completion", event, player_id, notification)
    if lead_changed_to:
        _enqueue_notification(session, "event_lead_change", event, player_id, {
            "team_id": team_id,
            "team_score": lead_changed_to[1],
            "task_id": task["id"],
            "task_label": task.get("label"),
        })
    return result


def _metric_path_room(session, task: dict, team_id, idx: int) -> bool:
    """Whether an any_path metric path can still absorb progress (its raw fold
    sits below its own need). The percent rollup floors, so "would the integer
    move" is the wrong question for metric rows — a 1-kill row on a 5,000-KC
    path must record even though the task percentage doesn't budge (progress
    re-folds from ledger rows; a dropped row is credit lost forever)."""
    from db.models import EventCompletion

    paths = (task.get("config") or {}).get("paths") or []
    if not (0 <= idx < len(paths)) or not isinstance(paths[idx], dict):
        return False
    need = _path_need(paths[idx])
    rows = (
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(("auto", "confirmed", "manual")))
        .all()
    )
    got = sum(
        max(int(r.quantity or 1), 1) for r in rows
        if _row_path_idx(r) == idx
        and (getattr(r, "source_type", None) or "") != "bonus"
    )
    return got < need


def _anypath_untagged_room(session, task: dict, team_id, candidate) -> bool:
    """Whether an UNTAGGED any_path item row (item-checklist or points path)
    still advances SOME path's own capped progress.

    Item and points paths share the untagged row pool but each has its own
    ``need``, so the global percentage (the closest path) is the wrong gate: a
    drop for a TRAILING path must still record or its credit is lost forever
    (progress re-folds from the ledger; a dropped row can never come back).
    Mirrors :func:`_metric_path_room` for tagged metric rows — per-path room,
    not "does the overall percentage move".

    A WILDCARD manual award (no matched item) always records — the admin
    mark-complete escape hatch, folded on the percent scale like a metric row."""
    if ((getattr(candidate, "source_type", None) or "") != "bonus"
            and not _norm(getattr(candidate, "matched_target", None))):
        return True
    from db.models import EventCompletion

    rows = [
        r for r in (
            session.query(EventCompletion)
            .filter(EventCompletion.task_id == task["id"],
                    EventCompletion.team_id == team_id,
                    EventCompletion.status.in_(("auto", "confirmed", "manual")))
            .all()
        )
        if _row_path_idx(r) is None  # untagged item rows only
    ]
    with_cand = rows + [candidate]
    for path in ((task.get("config") or {}).get("paths") or []):
        if not isinstance(path, dict) or path.get("metric") in PATH_METRICS:
            continue
        if _is_points_path(path):
            need = _path_need(path)
            weights = _path_point_weights(path)
            before = min(_points_fold(rows, weights), need)
            after = min(_points_fold(with_cand, weights), need)
        else:
            need = sum(n for _m, _nm, n in _parse_requirement_groups(path))
            if need <= 0:
                continue
            before = _grouped_progress_from_rows(rows, path, need)
            after = _grouped_progress_from_rows(with_cand, path, need)
        if after > before:
            return True
    return False


def _row_advances_progress(session, task: dict, team_id, candidate) -> bool:
    """Would this (unsaved) ledger row move the (task, team) rollup at all?

    Only item-list kinds with per-item/per-group needs can say "no": once a
    listed item is already satisfied, another copy of it is dead weight. For
    plain count/kc/xp folds every row advances progress until the threshold
    (the completed gate in :func:`record_match` handles the rest)."""
    if _pb_distinct_players(task):
        # A player who already beat the time is dead weight on a repeat kill.
        threshold = effective_threshold(session, task, team_id)
        return (_distinct_player_progress(session, task, team_id, threshold,
                                          include=candidate)
                > _distinct_player_progress(session, task, team_id, threshold))
    kind = _list_kind(task)
    if kind == "loot_sweep":
        # Dead weight once the item is capped AND no new set completes — the
        # running total wouldn't move, so don't record another popup.
        return (_loot_sweep_score(session, task, team_id, include=candidate)["total"]
                > _loot_sweep_score(session, task, team_id)["total"])
    if kind in ("all_of", "assembly"):
        helper = _distinct_item_progress
    elif kind == "groups":
        helper = _grouped_item_progress
    elif kind == "any_path":
        idx = _row_path_idx(candidate)
        if idx is not None:
            return _metric_path_room(session, task, team_id, idx)
        # Untagged item/points rows: per-path room, not the closest-path
        # percentage (a trailing path must never lose credit).
        return _anypath_untagged_room(session, task, team_id, candidate)
    else:
        return True
    return (helper(session, task, team_id, include=candidate)
            > helper(session, task, team_id))


# One physical acquisition can reach the queue twice: the drop submission and
# the collection-log submission it unlocks (separate guids, seconds apart), and
# both kinds match item_collection tasks — crediting the same item twice.
# Rows within this window are treated as echoes of the same acquisition.
CLOG_ECHO_WINDOW_SECONDS = 600


def _dedupe_clog_echo(session, task: dict, team_id, player_id,
                      kind, matched_target, quantity):
    """Cross-kind (drop <-> clog) dedupe for one item_collection acquisition.

    Returns the quantity to credit, or None to drop the row entirely.

    - An incoming ``clog`` row is skipped when a ledger row from the paired
      ``drop`` already credited the same (task, team, player, item) inside
      :data:`CLOG_ECHO_WINDOW_SECONDS` — the clog is the "new collection log
      slot" echo of that drop.
    - An incoming ``drop`` row (the rarer reverse ordering) is reduced by
      what its clog echo already credited, so stackable quantities still land
      (a 500-scales drop after its 1-unit clog credits the remaining 499);
      nothing left means the drop was fully pre-credited — skip.

    ``pending`` echoes count too: the acquisition is already represented in
    the ledger awaiting review, and confirming it later must not double-pay.
    """
    if task.get("type") != "item_collection" or not matched_target:
        return quantity
    if kind not in ("drop", "clog"):
        return quantity
    from db.models import EventCompletion

    other = "drop" if kind == "clog" else "clog"
    cutoff = datetime.now() - timedelta(seconds=CLOG_ECHO_WINDOW_SECONDS)
    echoes = (
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.player_id == player_id,
                EventCompletion.source_type == other,
                EventCompletion.status.in_(("auto", "confirmed", "manual", "pending")),
                EventCompletion.created_at >= cutoff)
        .all()
    )
    matched = [r for r in echoes if _norm(r.matched_target) == _norm(matched_target)]
    if not matched:
        return quantity
    if kind == "clog":
        return None
    remaining = int(quantity or 0) - sum(
        max(int(r.quantity or 1), 1) for r in matched)
    return remaining if remaining > 0 else None


def _dedupe_vestige_chain(session, task: dict, team_id, player_id, kind,
                          matched_target) -> bool:
    """True when this vestige credit may record; False when the player's
    ring/vestige chain already credited it (item_collection only).

    The DT2 pity mechanic makes the 1-ring, 2-ring and vestige drops (plus
    the vestige's clog echo) sightings of the SAME vestige-in-progress,
    spread over days — so one (task, team, player, vestige) credits ONCE,
    with no time window (unlike the 10-minute drop↔clog echo). Trade-off: a
    second full vestige cycle by the same player inside one event won't
    auto-credit again (astronomically rare; admins can award manually —
    manual rows neither block nor are blocked here).
    """
    if task.get("type") != "item_collection" or player_id is None:
        return True
    if kind not in ("drop", "clog"):
        return True
    target_norm = _norm(matched_target)
    if target_norm not in _VESTIGE_NORMS:
        return True
    from db.models import EventCompletion

    prior = (
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.player_id == player_id,
                EventCompletion.source_type.in_(("drop", "clog")),
                EventCompletion.status.in_(("auto", "confirmed", "manual", "pending")))
        .all()
    )
    return not any(
        _norm(r.matched_target) == target_norm
        and (r.source_type or "") in ("drop", "clog")
        for r in prior
    )


def record_match(session, redis_conn, event: dict, task: dict, team_id: int,
                 player_id: int, quantity: int, envelope: dict,
                 cells: Optional[list] = None,
                 matched_target: Optional[str] = None,
                 path_idx: Optional[int] = None) -> Optional[dict]:
    """Insert the ledger row for a match (idempotent on
    (task, team, submission_guid)); apply effects unless it needs
    confirmation. Returns a result dict, or None on duplicate replay.

    ``path_idx`` marks a metric-path match (any_path v2): the row is tagged
    with the path (``note`` = ``path:<idx>``) so the percent rollup folds it
    into the right path, and its guid gets a ``#p<idx>`` suffix — one envelope
    may legitimately write the item row AND one row per qualifying metric
    path, which the bare (task, team, guid) unique index would block.

    Rows that can no longer contribute are NOT recorded: once the (task,
    team) rollup is completed — or the specific matched item is already
    satisfied — another qualifying submission would only spam popups and
    pollute the contribution metrics, so it is dropped here, before the
    ledger insert / pending-review enqueue."""
    from db.models import EventCompletion, EventProgress

    progress_row = (session.query(EventProgress)
                    .filter(EventProgress.task_id == task["id"],
                            EventProgress.team_id == team_id)
                    .first())
    if progress_row is not None and progress_row.completed:
        return None

    # Vestige chain (rings + vestige + its clog = one acquisition, no time
    # window) — strictly stronger than the drop↔clog echo below for these.
    if not _dedupe_vestige_chain(session, task, team_id, player_id,
                                 envelope.get("kind"), matched_target):
        return None
    quantity = _dedupe_clog_echo(session, task, team_id, player_id,
                                 envelope.get("kind"), matched_target, quantity)
    if quantity is None:
        return None

    data = envelope.get("data") or {}
    status = completion_status(event, task, envelope)
    guid = envelope.get("guid")
    if guid and path_idx is not None:
        # Truncate the base BEFORE suffixing so a long guid (the WOM
        # reconciler's composite keys) can never shed the path marker.
        guid = f"{str(guid)[:60]}#p{int(path_idx)}"
    proof = data.get("image_url") or None
    completion = EventCompletion(
        event_id=event["id"],
        task_id=task["id"],
        team_id=team_id,
        player_id=player_id,
        status=status,
        quantity=max(int(quantity or 1), 1),
        source_type=envelope.get("kind"),
        source_id=data.get("source_id"),
        submission_guid=str(guid)[:64] if guid else None,
        proof_url=str(proof)[:255] if proof else None,
        matched_target=matched_target,
        note=_path_note(path_idx) if path_idx is not None else None,
    )
    if not _row_advances_progress(session, task, team_id, completion):
        return None  # matched item already satisfied — contributes nothing
    try:
        with session.begin_nested():
            session.add(completion)
            session.flush()
    except IntegrityError:
        return None  # replay of the same submission — idempotent no-op
    except DataError:
        # Out-of-range value (e.g. a quantity beyond the column type before
        # web58a widened it) — skip THIS row rather than dead-letter the whole
        # envelope, and say so loudly.
        import logging

        logging.getLogger(__name__).exception(
            "Ledger insert out of range: event=%s task=%s team=%s qty=%s",
            event["id"], task["id"], team_id, quantity)
        return None

    player_name = envelope.get("player_name")
    if status == "pending":
        frame = {
            "kind": "pending", "event_id": event["id"], "task_id": task["id"],
            "team_id": team_id, "player_id": player_id,
        }
        if player_name:
            frame["player_name"] = player_name
        # Projection (web53a): lets the board tint the tile amber live — the
        # flushed row above is visible to the query.
        try:
            proj = pending_projection(session, task, team_id)
            if proj:
                frame["pending"] = proj["pending_count"]
                frame["pending_complete"] = proj["pending_complete"]
                frame["progress"] = proj["applied"]
        except Exception:
            pass
        _publish(event["id"], frame)
        _enqueue_notification(session, "event_pending", event, player_id, {
            "task_id": task["id"],
            "task_label": task.get("label"),
            "team_id": team_id,
            "player_id": player_id,
            "player_name": player_name,
            "completion_id": completion.id,
            "source_type": completion.source_type,
            "proof_url": completion.proof_url,
        })
        return {"kind": "pending", "event_id": event["id"],
                "task_id": task["id"], "team_id": team_id,
                "completion_id": completion.id}

    return apply_ledger_row(session, redis_conn, event, task, completion,
                            cells=cells, player_name=player_name)


def handle_envelope(session, redis_conn, state: MatcherState, envelope: dict,
                    staged: Optional[StagedWrites] = None) -> list:
    """Evaluate one queue envelope against the matcher state. Returns the list
    of result dicts (empty when nothing matched). Caller commits.

    Pass a :class:`StagedWrites` to defer the watermark/baseline/dedupe Redis
    advances until after that commit (P0-6) — the caller then flushes it on
    success and simply drops it on rollback."""
    results = []
    try:
        player_id = int(envelope.get("player_id"))
    except (TypeError, ValueError):
        return results
    memberships = state.participants.get(player_id) or []
    if not memberships:
        return results
    try:
        submitted_at = datetime.fromtimestamp(int(envelope.get("ts") or time.time()))
    except (TypeError, ValueError, OSError, OverflowError):
        submitted_at = datetime.now()

    for event_id, team_id, joined_at in memberships:
        event = state.events.get(event_id)
        if event is None:
            continue
        # PRD D10: joined_at is the credit cutoff; window rules (A5) freeze
        # evaluation outside the active window.
        if joined_at is not None and submitted_at < joined_at:
            continue
        if event["window_start"] is not None and submitted_at < event["window_start"]:
            continue
        if event["window_end"] is not None and submitted_at > event["window_end"]:
            continue
        if not accepts_submission_source(event, envelope):
            continue

        # Board-game events (web44a): a team only ever works its CURRENT
        # tile task — no live task (awaiting roll / blocked / finished)
        # means nothing can match for this event.
        board_task_id = None
        if event.get("kind") == "board_game":
            board_task_id = state.board_task_by_team.get((event_id, team_id))
            if board_task_id is None:
                continue

        # Bingo events: only tasks bound to a board tile are "active". A task
        # that sits in the event's task list but isn't placed on any cell
        # marks no tile when it completes, so tracking it just fires completion
        # popups with nothing on the board to show for them (the reported
        # confusion: "completion messages but none of the tiles were marked").
        # Restrict matching to cell-bound tasks. Board-game events have their
        # own current-tile filter above and are excluded here.
        bingo_board = event.get("has_bingo") and event.get("kind") != "board_game"

        xp_delta = None  # baseline folded at most once per (event, envelope)
        for task in state.tasks_by_event.get(event_id, []):
            if board_task_id is not None and task["id"] != board_task_id:
                continue
            if bingo_board and task["id"] not in state.cells_by_task:
                continue
            # One envelope may yield several matches on ONE task (any_path v2:
            # the drop that advances an item path is also a kill for a KC path
            # and GP for a loot-value path) — each records its own ledger row.
            for match in match_task_all(task, envelope):
                quantity = match["quantity"]
                data = envelope.get("data") or {}
                # WOM envelopes carry the window-start value; seeding from it
                # is only valid for the event whose window produced it, and
                # only for players in before the window opened (PRD D10).
                wom_seed_ok = (data.get("source") == "wom"
                               and data.get("target_event_id") == event_id
                               and _seed_allowed(joined_at, event["window_start"]))
                if match["mode"] == "kc":
                    # Multi-NPC kc tasks keep absolute-KC state PER NPC — each
                    # NPC's kill_count is its own counter, so one shared
                    # watermark would swallow the lower counts. Metric-path
                    # matches scope per (task, path, NPC) on top.
                    kc_scope = _match_kc_scope(task, match, _norm(data.get("npc_name")))
                    try:
                        kill_count = int(data.get("kill_count"))
                    except (TypeError, ValueError):
                        kill_count = None
                    if kill_count is not None and kill_count > 0:
                        quantity = _fold_kc_watermark(
                            redis_conn, event_id, kc_scope, player_id,
                            kill_count, first_credit_offset=1, staged=staged)
                        if quantity <= 0:
                            continue
                    else:
                        # No usable absolute KC: cooldown dedupe as before, and
                        # note the credit so a later absolute fold subtracts it.
                        if not _kc_dedupe(redis_conn, event_id, kc_scope,
                                          player_id, envelope, staged=staged):
                            continue
                        try:
                            fb_key = _kc_fallback_key(event_id, kc_scope, player_id)
                            if staged is not None:
                                staged.stage("incr", fb_key)
                                staged.stage("expire", fb_key, _STATE_KEY_TTL)
                            else:
                                redis_conn.incr(fb_key)
                                redis_conn.expire(fb_key, _STATE_KEY_TTL)
                        except Exception:
                            pass
                elif match["mode"] == "kc_abs":
                    metric = str(data.get("boss_metric") or "").strip().lower()
                    kc_scope = _match_kc_scope(
                        task, match, _match_wom_npc(task, match, metric))
                    quantity = _fold_kc_watermark(
                        redis_conn, event_id, kc_scope, player_id, data.get("kc"),
                        seed=data.get("kc_start") if wom_seed_ok else None,
                        staged=staged)
                    if quantity <= 0:
                        continue
                elif match["mode"] == "xp":
                    if xp_delta is None:
                        xp_delta = _fold_xp_baseline(
                            redis_conn, event_id, player_id,
                            data.get("skill"), data.get("xp"),
                            seed=data.get("xp_start") if wom_seed_ok else None,
                            staged=staged)
                    if xp_delta <= 0:
                        continue
                    quantity = xp_delta
                outcome = record_match(
                    session, redis_conn, event, task, team_id, player_id,
                    quantity, envelope, cells=state.cells_by_task.get(task["id"]),
                    matched_target=match.get("matched_target"),
                    path_idx=match.get("path"))
                if outcome is not None:
                    results.append(outcome)
    return results


def apply_completion(session, completion) -> Optional[dict]:
    """Apply a ledger row loaded from the DB (Task 18 confirmation flow).

    Loads the event/task/cells context and delegates to
    :func:`apply_ledger_row`. Caller flips ``completion.status`` (e.g. to
    ``confirmed``) and owns the commit.
    """
    from db.models import Event, EventTask, EventBingoCell
    from utils.redis import redis_client

    event = session.query(Event).filter(Event.id == completion.event_id).first()
    task = session.query(EventTask).filter(EventTask.id == completion.task_id).first()
    if event is None or task is None:
        return None
    cells = [
        {"id": c.id, "idx": c.idx, "event_id": c.event_id, "label": c.label}
        for c in session.query(EventBingoCell).filter(
            EventBingoCell.task_id == task.id).all()
    ]
    return apply_ledger_row(
        session, getattr(redis_client, "client", None),
        _event_to_dict(event), _task_to_dict(task), completion, cells=cells)


def _unwind_bonuses(session, event: dict, team_id: int) -> list:
    """Revoke applied bonus ledger rows whose line/blackout no longer holds
    (Task 20 revoke cascade). The still-valid set is derived from the team's
    current cell completions; each stale row is flipped to ``revoked`` and the
    points it stored in ``quantity`` are subtracted from the team score.
    Returns the notes revoked (e.g. ``["line:r0", "blackout"]``)."""
    from db.models import EventBingoCell, EventCompletion, EventTeam

    bonus_rows = (session.query(EventCompletion)
                  .filter(EventCompletion.event_id == event["id"],
                          EventCompletion.team_id == team_id,
                          EventCompletion.source_type == "bonus",
                          EventCompletion.status.in_(APPLIED_BONUS_STATUSES))
                  .all())
    if not bonus_rows:
        return []
    cells = session.query(EventBingoCell).filter(
        EventBingoCell.event_id == event["id"]).all()
    size = _board_size_for(event, cells)
    still_valid = set()
    if size:
        done_idxs = _team_completed_idxs(session, cells, team_id)
        still_valid = {f"line:{key}" for key in completed_lines(size, done_idxs)}
        if cells and len(done_idxs) == len(cells):
            still_valid.add(BLACKOUT_NOTE)

    revoked, delta = [], 0
    for row in bonus_rows:
        if row.note in still_valid:
            continue
        row.status = "revoked"
        delta += max(int(row.quantity or 0), 0)
        revoked.append(row.note)
    if delta:
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = int(team.score or 0) - delta
    return revoked


def revoke_ledger_row(session, completion) -> Optional[dict]:
    """Recompute (task, team) state after a ledger row was revoked (Task 18).

    The caller has already flipped ``completion.status`` to ``revoked`` and
    owns the commit. This re-folds the progress rollup from surviving ledger
    rows (``auto``/``confirmed``/``manual``), adjusts the team score by the
    delta of ``completed × points``, removes the team's bingo-cell completions
    for the task's cells when the task is no longer complete, revokes bonus
    ledger rows for lines the team no longer holds (Task 20), and publishes an
    SSE correction (``kind: "revoke"``). Returns a summary dict for the
    caller's audit row (None when the event/task rows are gone).
    """
    from db.models import (Event, EventBingoCell, EventBingoCompletion,
                           EventCompletion, EventProgress, EventTask, EventTeam)

    event_row = session.query(Event).filter(Event.id == completion.event_id).first()
    task_row = session.query(EventTask).filter(EventTask.id == completion.task_id).first()
    if event_row is None or task_row is None:
        return None
    event = _event_to_dict(event_row)
    task = _task_to_dict(task_row)
    team_id = completion.team_id

    if (completion.source_type or "") == "bonus":
        # A directly-revoked bonus row has no progress/cell state of its own —
        # just take back exactly the points it granted (stored in quantity).
        team_score = None
        if team_id is not None:
            team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
            if team is not None:
                team.score = int(team.score or 0) - max(int(completion.quantity or 0), 0)
                team_score = team.score
        session.flush()
        frame = {"kind": "revoke", "event_id": event["id"], "task_id": task["id"],
                 "team_id": team_id, "bonus": completion.note}
        if team_score is not None:
            frame["team_score"] = team_score
        _publish(event["id"], frame)
        return {"progress": None, "completed": None, "team_score": team_score,
                "revoked_bonuses": [completion.note]}

    if _list_kind(task) == "loot_sweep":
        return _revoke_loot_sweep(session, event, task, team_id, completion)

    if _pb_distinct_players(task):
        new_progress = _distinct_player_progress(
            session, task, team_id, effective_threshold(session, task, team_id))
    elif _list_kind(task) in ("all_of", "assembly"):
        # Distinct-item semantics (the revoked row is already excluded — its
        # status flipped before this recompute).
        new_progress = _distinct_item_progress(session, task, team_id)
    elif _list_kind(task) == "groups":
        new_progress = _grouped_item_progress(session, task, team_id)
    elif _list_kind(task) == "any_path":
        new_progress = _anypath_item_progress(session, task, team_id)
    else:
        survivors = (session.query(EventCompletion)
                     .filter(EventCompletion.task_id == task["id"],
                             EventCompletion.team_id == team_id,
                             EventCompletion.status.in_(("auto", "confirmed", "manual")))
                     .all())
        new_progress = sum(
            max(int(r.quantity or 1), 1) for r in survivors
            if (r.source_type or "") != "bonus"
        )

    # P0-7: locked read — progress is a read-modify-write shared between the
    # consumer and webapi confirm/revoke flows; unlocked, one side's update
    # silently overwrites the other's under REPEATABLE READ.
    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
                .with_for_update()
                .first())
    was_completed = bool(progress.completed) if progress is not None else False
    threshold = effective_threshold(session, task, team_id)
    now_completed = new_progress >= threshold if new_progress > 0 else False
    if progress is None:
        if new_progress <= 0:
            return {"progress": 0, "completed": False, "team_score": None}
        progress = EventProgress(event_id=event["id"], task_id=task["id"],
                                 team_id=team_id, progress=0, completed=False)
        session.add(progress)
    progress.progress = new_progress
    progress.completed = now_completed
    if not now_completed:
        progress.completed_at = None

    team_score = None
    points = int(task.get("points") or 0)
    if points and team_id is not None and was_completed != now_completed:
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = int(team.score or 0) + (points if now_completed else -points)
            team_score = team.score

    # Keep per-player contribution points honest: still complete → shares are
    # redistributed over the surviving ledger; no longer complete → deleted.
    if now_completed:
        _award_contribution_points(
            session, event, task, team_id,
            _task_contributors(session, task["id"], team_id), points)
    elif was_completed:
        from db.models import EventPlayerPoints
        (session.query(EventPlayerPoints)
         .filter(EventPlayerPoints.task_id == task["id"],
                 EventPlayerPoints.team_id == team_id)
         .delete(synchronize_session=False))

    if was_completed and not now_completed and event.get("has_bingo") and team_id is not None:
        cell_ids = [
            c.id for c in session.query(EventBingoCell)
            .filter(EventBingoCell.task_id == task["id"]).all()
        ]
        if cell_ids:
            (session.query(EventBingoCompletion)
             .filter(EventBingoCompletion.cell_id.in_(cell_ids),
                     EventBingoCompletion.team_id == team_id)
             .delete(synchronize_session=False))

    # Un-completed cells may break lines/blackout: revoke the team's stale
    # bonus rows and take back their points (Task 20).
    revoked_bonuses = []
    if event.get("has_bingo") and team_id is not None:
        revoked_bonuses = _unwind_bonuses(session, event, team_id)
        if revoked_bonuses:
            team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
            if team is not None:
                team_score = int(team.score or 0)

    session.flush()
    frame = {
        "kind": "revoke", "event_id": event["id"], "task_id": task["id"],
        "team_id": team_id, "progress": new_progress, "target": threshold,
        "completed": now_completed,
    }
    if team_score is not None:
        frame["team_score"] = team_score
    if revoked_bonuses:
        frame["revoked_bonuses"] = revoked_bonuses
    _publish(event["id"], frame)
    return {"progress": new_progress, "completed": now_completed,
            "team_score": team_score, "revoked_bonuses": revoked_bonuses}
