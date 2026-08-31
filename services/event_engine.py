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

``used_api`` means "came from the RuneLite plugin" — true for both the
direct plugin API and the Discord webhook-bot intake (the plugin posts
those embeds too); false only for manual website/command submissions.
Events gate on it via ``submission_policy`` (``all`` / ``confirm_non_api``
/ ``api_only``). Absent (pre-upgrade queue entries) is treated as
non-plugin.

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
  (default 1 → completes on the first qualifying pet). DUPLICATE pets are
  refused (:func:`_pet_is_new`) — this counts acquisitions, so a re-drop of a
  pet the player owns is not one. ``loot_sweep`` pet entries refuse them too
  (status quo hold, see there); ``item_collection`` ``pet_items`` ACCEPTS
  them — a "5 of these 4 items" tile needs the duplicate.
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

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import DataError, IntegrityError

from utils import task_progress as _tp

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
# Per-event "it's over" tombstone, stamped the moment an event ends (manual or
# scheduled — services/event_lifecycle._mark_active_in_redis). The matcher's
# state snapshot can stay stale for up to STATE_REFRESH_SECONDS after an end,
# and for a PREMATURE manual end the window check can't help (ends_at is
# still in the future) — the tombstone is what makes an end take effect
# immediately instead of after the next refresh. Deliberately a negative
# gate: absence means "process normally", so a lost key (Redis restart) can
# only reopen the ≤30s race, never silently drop earned credit.
ENDED_TOMBSTONE_KEY = "events:ended:{event_id}"
ENDED_TOMBSTONE_TTL_SECONDS = 48 * 3600
# The team an ``event_lead_change`` was last ANNOUNCED for — the only place a
# previous leader is persisted, and the cross-lane claim that keeps concurrent
# applies from each announcing the same hand-over (see _claim_lead_change).
# The TTL bounds the keyspace; it is refreshed on every claim and a missing key
# announces, so expiry costs nothing.
LEAD_WATERMARK_KEY = "events:{event_id}:leadteam"
LEAD_WATERMARK_TTL_SECONDS = 30 * 24 * 3600
ADMIN_BUMP_CHANNEL = "rt:event-admin"      # pubsub bump on event/task/roster mutations

_STATE_KEY_TTL = 60 * 60 * 24 * 60         # 60 days for xp-baseline / kc-dedupe keys

# Task types the engine can evaluate automatically (v1).
AUTO_TASK_TYPES = ("item_collection", "kc_target", "pb_target", "xp_target", "skill_target",
                   "loot_value", "pet_collection", "ca_target", "loot_sweep",
                   "competition")


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
    from utils.mirror_context import skip_mirrored_extras

    # Mirrored production traffic: dev's database is a production dump, so this
    # would apply progress to real clans' live events (in dev's copy of them) at
    # production rates. Contained, but it buries whatever is being tested.
    # MIRROR_PROCESS_EXTRAS=true opts in when soaking the event engine is the point.
    if skip_mirrored_extras():
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


def set_ended_tombstone(redis_conn, event_id) -> None:
    """Stamp the per-event ended tombstone (see ``ENDED_TOMBSTONE_KEY``).
    Best-effort — the DB status flip is the durable record."""
    if redis_conn is None:
        return
    try:
        redis_conn.set(
            ENDED_TOMBSTONE_KEY.format(event_id=int(event_id)),
            int(time.time()), ex=ENDED_TOMBSTONE_TTL_SECONDS,
        )
    except Exception:
        pass


def clear_ended_tombstone(redis_conn, event_id) -> None:
    if redis_conn is None:
        return
    try:
        redis_conn.delete(ENDED_TOMBSTONE_KEY.format(event_id=int(event_id)))
    except Exception:
        pass


def is_event_ended(redis_conn, event_id) -> bool:
    """True when ``event_id`` carries the ended tombstone. Fail-open: any
    doubt (no conn, Redis error, test stub without ``exists``) reads as "not
    ended" — the DB-derived matcher state stays authoritative, and a skipped
    envelope is consumed, so a false positive here would drop earned credit."""
    if redis_conn is None:
        return False
    try:
        return bool(int(redis_conn.exists(
            ENDED_TOMBSTONE_KEY.format(event_id=int(event_id)))))
    except Exception:
        return False


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
    # str, not the bare int: callers append the recurring-window suffix
    # (``window_scope``) to this, and ``int + str`` raises — which crashed the
    # whole envelope, so every WOM KC update for a single-NPC task was
    # dead-lettered instead of credited. The rendered key is unchanged (every
    # consumer interpolates this into an f-string), so live state survives.
    if len(_kc_npcs(task)) <= 1:
        return str(task["id"])
    return f"{task['id']}:{npc_norm.replace(' ', '_')}"


# ── any_path metric paths (v2: "boss pet OR 5,000 KC") ────────────────────────

# Path metrics an any_path config may carry besides item groups. ``kc`` counts
# kills of the path's NPCs (kc_target semantics: watermark + dedupe, WOM
# reconciliation); ``loot_value`` folds drop GP (optionally NPC-scoped).
# These folds now live in utils.task_progress, shared with
# services.competition (a service may not import a sibling service — the
# pytest conftest MagicMocks the whole package). The private names in this
# module stay as aliases so its ~40 call sites are untouched.
PATH_METRICS = _tp.PATH_METRICS

_PATH_NOTE_PREFIX = "path:"


def _path_note(idx: int, note: Optional[str] = None) -> str:
    """Engine-written ledger tag binding a metric row to its config path.
    A manual award may carry an admin note alongside the tag
    (``path:N | reason``) — :func:`_row_path_idx` parses either form and
    :func:`display_note` recovers the human half."""
    tag = f"{_PATH_NOTE_PREFIX}{int(idx)}"
    return f"{tag} | {note}" if note else tag


_row_path_idx = _tp.row_path_idx


def display_note(note) -> Optional[str]:
    """Human-facing half of a ledger ``note``: the admin's free text with any
    engine ``path:N`` tag stripped. None when the note is empty or is a bare
    tag — serializers use this so path bookkeeping never leaks into the UI."""
    if not isinstance(note, str) or not note.strip():
        return None
    if note.startswith(_PATH_NOTE_PREFIX):
        head, sep, rest = note[len(_PATH_NOTE_PREFIX):].partition("|")
        try:
            int(head.strip())
        except (TypeError, ValueError):
            return note.strip()  # free text that merely starts with "path:"
        return rest.strip() or None if sep else None
    return note.strip()


_path_need = _tp.path_need


def _metric_paths_from_config(config: dict, resolve_wom: bool = False) -> tuple:
    """Normalized metric-path entries of an ``any_path`` config:
    ``({"idx", "metric", "npcs": frozenset, "need", "min_value", "min_strict"
    [, "wom_metrics"]}, ...)``.
    ``idx`` is the position in ``config.paths`` (item paths keep their slots).
    ``resolve_wom`` additionally maps each kc path's NPCs to WOM hiscores
    metrics (state-load only — it does name lookups)."""
    out = []
    for idx, path in enumerate((config or {}).get("paths") or []):
        if not isinstance(path, dict) or path.get("metric") not in PATH_METRICS:
            continue
        min_value, min_strict = _min_value_gate(path)
        entry = {"idx": idx, "metric": path["metric"],
                 "npcs": _norm_npc_set(path.get("npcs")), "need": _path_need(path),
                 "min_value": min_value, "min_strict": min_strict}
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


# ── cumulative-GP minimum drop value (t58) ────────────────────────────────────


def _min_value_gate(scope: dict) -> tuple:
    """``(min_value, strict)`` of a cumulative-GP scope — a ``loot_value``
    task's ``config`` or one ``any_path`` loot-value path.

    ``min_value`` 0 (absent, or garbage that can't be read as a count of GP)
    means unset: every drop counts and follows the normal review policy, which
    is exactly the behaviour that predates this gate. Set, sub-threshold drops
    still fold into the GP total but skip review (:func:`completion_status`) —
    the total stays honest while the queue stays free of bones-and-ashes
    traffic — unless ``min_value_strict``, which drops them from the match
    entirely so they never count at all."""
    if not isinstance(scope, dict):
        return 0, False
    try:
        min_value = max(int(scope.get("min_value") or 0), 0)
    except (TypeError, ValueError):
        return 0, False
    return min_value, bool(min_value and scope.get("min_value_strict"))


def _loot_min_value(task: dict, path_idx: Optional[int] = None) -> tuple:
    """The ``(min_value, strict)`` gate one cumulative-GP match answers to: the
    ``loot_value`` task's own config, or the ``any_path`` loot-value path at
    ``path_idx``. ``(0, False)`` — unset — for every other task/path shape."""
    if path_idx is None:
        if task.get("type") != "loot_value":
            return 0, False
        return _min_value_gate(task.get("config") or {})
    for path in _metric_paths(task):
        if path.get("idx") == path_idx and path.get("metric") == "loot_value":
            return int(path.get("min_value") or 0), bool(path.get("min_strict"))
    return 0, False


def _drop_value(envelope: dict) -> Optional[int]:
    """GP value of a drop envelope, or None when it isn't a drop / carries no
    readable value (the value-aware gates then stay out of the way)."""
    if envelope.get("kind") != "drop":
        return None
    try:
        return int((envelope.get("data") or {}).get("total_value") or 0)
    except (TypeError, ValueError):
        return None


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


def _pet_is_new(data: dict) -> bool:
    """Whether a ``pet`` envelope is a pet the player did not already own.

    The producer sends every pet submission now, duplicates included, because
    an ``item_collection`` tile listing a pet in ``config.pet_items`` is only
    satisfiable with them ("5 of these 4 items"). Task types that count
    ACQUISITIONS rather than drops screen duplicates out with this.

    An absent flag means an envelope queued before the producer sent
    duplicates at all — it was new by construction, so it reads as new."""
    return bool((data or {}).get("is_new_pet", True))


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


# Continuous-metric task types whose plugin progress fan-out stays stepped
# even under a per-task progress_notify of 'all': every XP drop / loot stack
# is an increment, so per-increment envelopes would spam every teammate's
# game chat. (The default for every type is 25/50/75% milestones only.)
PLUGIN_PROGRESS_STEP_TASK_TYPES = ("xp_target", "loot_value")
PLUGIN_PROGRESS_STEP_PCT = 10


def _plugin_progress_step_crossed(previous: int, current: int, threshold: int) -> bool:
    """True when previous→current crosses a PLUGIN_PROGRESS_STEP_PCT boundary
    of the threshold (integer math: which 10%-bucket each value sits in)."""
    if threshold <= 0:
        return True
    steps = 100 // PLUGIN_PROGRESS_STEP_PCT
    return (current * steps) // threshold > (previous * steps) // threshold


def task_progress_notify_mode(task: dict) -> Optional[str]:
    """Per-task progress-notification override (``config.progress_notify``:
    'off' | 'milestones' | 'all'), or None to inherit the event/team modes.
    Set per tile so a bulk-quantity tile ("15k granite dust") can announce at
    25/50/75% while a set tile ("full inquisitor") announces every piece."""
    mode = parse_task_config(task.get("config")).get("progress_notify")
    return mode if mode in ("off", "milestones", "all") else None


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


_distinct_progress_from_rows = _tp.distinct_progress_from_rows


_distinct_players_from_rows = _tp.distinct_players_from_rows


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


_parse_requirement_groups = _tp.parse_requirement_groups


_grouped_progress_from_rows = _tp.grouped_progress_from_rows


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


_is_points_path = _tp.is_points_path
_path_point_weights = _tp.path_point_weights
_points_fold = _tp.points_fold


_anypath_progress_from_rows = _tp.anypath_progress_from_rows


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
    if kind == "competition":
        # "applied"/"projected" are the team's ranking-mode totals (points or
        # gained); a competition never completes either.
        from services.competition import CompetitionConfig, fold_rows, team_totals
        cfg = CompetitionConfig(task.get("config") or {})
        return {
            "applied": team_totals(fold_rows(applied_rows, cfg), cfg)[1],
            "projected": team_totals(fold_rows(rows, cfg), cfg)[1],
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
        if allowed:
            if _norm(data.get("npc_name")) not in allowed:
                return None
            # ``clog_sources`` opts a config into letting the collection-log
            # unlock count as well, when the clog names an allowed source. A
            # restricted item is otherwise drop-only, which is right for a
            # "get it from THIS boss" task but wrong for a competition bonus:
            # filling a Vorkath log slot genuinely is a Vorkath achievement.
            # Off by default — existing tasks keep drop-only semantics.
            if kind != "drop" and not (task.get("config") or {}).get("clog_sources"):
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
            if not _pet_is_new(data):
                # Status quo hold, not a considered scoring rule: duplicates
                # never reached the engine before the producer stopped gating
                # them, and enabling them here would silently change scoring in
                # a running sweep. Whether a duplicate pet is worth sweep points
                # is an open policy call — delete these two lines to allow it.
                return None
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
        # ``config.min_value`` only skips REVIEW for sub-threshold drops (they
        # still count — see completion_status); the opt-in strict flag is the
        # one that stops them counting.
        min_value, min_strict = _min_value_gate(config)
        if min_strict and value < min_value:
            return None
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
        if not _pet_is_new(data):
            # A DUPLICATE pet is not a collected pet: "obtain 3 boss pets" must
            # not be satisfiable by re-killing one boss for a pet you already
            # own. The producer emits duplicates now (item_collection tiles
            # need them), so the gate lives here.
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

    if task_type == "ca_target":
        # Combat achievements. The producer (data/submissions/ca.py) only
        # queues NEW completions, so every envelope that gets here is a first
        # completion and no dedupe of its own is needed.
        #
        # ``config.task_names`` is an explicit allow-list RESOLVED AT AUTHORING
        # from the task registry's ``monster``/``tier`` fields
        # (utils.ca_tasks.tasks_for_monsters) — the same shape as a customized
        # pet list, and for the same reason: the registry lives in the database
        # and this function is pure. That resolution is what lets a combat
        # achievement be scoped to a boss at all; the envelope itself carries
        # only ``task_name`` and ``tier``.
        if kind != "ca":
            return None
        task_name = data.get("task_name")
        if not task_name:
            return None
        config = task.get("config") or {}
        target = task.get("target")
        if target:
            if _norm(task_name) != _norm(target):
                return None
        else:
            listed = task.get("ca_name_set")
            if listed is None:
                listed = {_norm(n) for n in (config.get("task_names") or ())}
            if not listed:
                # An unresolvable allow-list must credit NOTHING. The write
                # validator refuses to store an empty one, so reaching here
                # means the config was hand-built or the registry was empty
                # when it was written — either way, crediting every CA in the
                # game is the one answer that is certainly wrong.
                return None
            if _norm(task_name) not in listed:
                return None
        return {"mode": "count", "quantity": 1,
                "matched_target": str(task_name).strip()[:120] or None}

    if task_type == "competition":
        # SOTW/BOTW race task (services/competition.py). ``competition`` is
        # the plain-data matcher snapshot precomputed in _task_to_dict.
        # Gained rides the existing folds (xp baselines / kc watermarks);
        # a pet bonus is a normal count row tagged via ``bonus`` (the
        # time_under bonus lives in match_task_all — one pb envelope can
        # award several time tiers at once).
        comp = task.get("competition") or {}
        metric_kind = comp.get("metric_kind")
        if not metric_kind:
            return None
        if kind == "experience":
            if metric_kind != "skill":
                return None
            skill = comp.get("skill")
            if not skill or _norm(data.get("skill")) != skill:
                return None
            return {"mode": "xp", "quantity": 0}
        if kind == "drop":
            if metric_kind != "boss":
                return None
            if _norm(data.get("npc_name")) not in (comp.get("npcs") or ()):
                return None
            return {"mode": "kc", "quantity": 1}
        if kind == "wom_kc":
            if metric_kind != "boss":
                return None
            metric = str(data.get("boss_metric") or "").strip().lower()
            if not metric or metric not in _kc_wom_metrics(task):
                return None
            return {"mode": "kc_abs", "quantity": 0}
        if kind == "pet":
            # A DUPLICATE pet is not an achievement worth bonus points — and
            # the per-player cap must not be burnable by re-rolling a pet the
            # player already owns.
            if not _pet_is_new(data):
                return None
            raw_name = data.get("pet_name") or data.get("item_name")
            rule = (comp.get("pet_rules") or {}).get(_norm(raw_name))
            if not rule:
                return None
            return {"mode": "count",
                    "quantity": max(int(rule.get("points") or 1), 1),
                    "matched_target": str(raw_name).strip()[:120] or None,
                    "bonus": {"rule_id": rule.get("id"), "type": "pet"}}
        return None

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
    comp = task.get("competition") if task.get("type") == "competition" else None
    if comp:
        # time_under bonus rules: EVERY kill time reaches the queue (not just
        # PBs — data/submissions/pb.py), and tiers stack deliberately — a
        # 0:48 kill under both "sub-1:00" and "sub-0:50" rules awards both
        # (each rule is its own ledger row, tagged with its rule id).
        if envelope.get("kind") == "pb":
            data = envelope.get("data") or {}
            npc = _norm(data.get("npc_name"))
            try:
                time_ms = int(data.get("time_ms") or 0)
            except (TypeError, ValueError):
                time_ms = 0
            if npc and time_ms > 0:
                for rule in comp.get("time_rules") or ():
                    threshold = int(rule.get("threshold_ms") or 0)
                    if npc == rule.get("npc") and threshold and time_ms <= threshold:
                        matches.append({
                            "mode": "count",
                            "quantity": max(int(rule.get("points") or 1), 1),
                            "matched_target": str(data.get("npc_name") or "").strip()[:120] or None,
                            "bonus": {"rule_id": rule.get("id"),
                                      "type": "time_under",
                                      "time_ms": time_ms},
                        })
        # ``task`` bonus rules: the criteria ARE a task config, so run the same
        # match_task over the synthetic dict _task_to_dict built from it. Every
        # shape the builder can express — a single drop, an all-of set, an
        # either-or, a weighted pool, a collection-log slot, a fast kill, a
        # combat achievement — comes back for free, already NPC-scoped by the
        # source restriction the write validator injected.
        kind = envelope.get("kind")
        for rule in comp.get("task_rules") or ():
            kinds = rule.get("kinds") or ()
            if kinds and kind not in kinds:
                continue
            embedded = rule.get("task")
            if not isinstance(embedded, dict):
                continue
            hit = match_task(embedded, envelope)
            if hit is None:
                continue
            if hit.get("mode") not in ("count", "first"):
                # Load-bearing, not defensive tidiness: a kc/kc_abs/xp match
                # would reach handle_envelope's shared KC-watermark and
                # XP-baseline folds, which are scoped by the TASK — and the
                # task here is the race itself, so a bonus rule would clobber
                # the very counter it is supposed to sit beside. The write
                # validator refuses those embedded types; this is the second
                # lock on the same door.
                continue
            matches.append({
                "mode": "count",
                # Credit UNITS, not points: the fold converts at
                # ``points`` per ``need`` (services/competition.py).
                "quantity": max(int(hit.get("quantity") or 1), 1),
                "matched_target": hit.get("matched_target"),
                "bonus": {"rule_id": rule.get("id"), "type": "task"},
            })
        return matches
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
                # Sub-threshold drops still count (they only skip review) —
                # unless the path opted into the strict gate.
                if path.get("min_strict") and value < int(path.get("min_value") or 0):
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
    path (pure; no I/O). Only ``api_only`` rejects; ``used_api`` means "came
    from the plugin" (API or Discord-webhook intake — producers stamp it;
    manual submissions are the only non-plugin traffic). A missing flag
    (pre-upgrade queue entries) reads as non-plugin. WOM-reconciler envelopes
    are hiscores-sourced server-side data — trusted under every policy."""
    if envelope.get("source") == "wom":
        return True
    if event.get("submission_policy") == "api_only":
        return bool(envelope.get("used_api"))
    return True


def completion_status(event: dict, task: dict, envelope: dict,
                      path_idx: Optional[int] = None) -> str:
    """Initial ledger-row status for a match (pure; no I/O): ``pending`` when
    the task or event forces confirmation (PRD D3), or when the event's
    ``confirm_non_api`` policy holds a submission that didn't come from the
    plugin (manual web/command submissions; plugin traffic via either the
    API or the Discord webhook reader auto-applies); ``auto`` otherwise.

    One exception, and it is deliberate (t58): a cumulative-GP scope with a
    ``min_value`` auto-applies drops UNDER that threshold whatever the review
    policy says. A "10B GP" tile reviewed drop-by-drop drowns its admins in
    bones and ashes, so the threshold is the admin's own statement of what is
    worth looking at — the small stuff still counts toward the total, it just
    doesn't queue. ``path_idx`` picks the any_path metric path the match was
    made against (None = the task's own config)."""
    min_value, _ = _loot_min_value(task, path_idx)
    if min_value:
        value = _drop_value(envelope)
        if value is not None and value < min_value:
            return "auto"
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
    # Bingo EHB: event_id -> {normalized npc name -> {npc_id, metric, tasks}}.
    # The NPCs an event's tasks make relevant, so effort can be recorded for
    # kills that credit nothing. Resolved via load_effort_npcs (Redis-cached).
    effort_npcs_by_event: dict = field(default_factory=dict)
    # (event_id, team_id) -> set of task ids already complete. The effort
    # freeze gate, mirrored into Redis for the hot path.
    completed_tasks_by_team: dict = field(default_factory=dict)
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
        # Recurring schedules (web82a): set truthy when schedule_config is
        # present; load_matcher_state attaches the materialized "windows"
        # list ([(start, end), ...]) for the scoring gate.
        "scheduled": bool(getattr(event, "schedule_config", None)),
    }


#: Returned by :func:`schedule_window_seq` for a continuous (unscheduled)
#: event — distinct from ``None`` ("scheduled, but closed right now").
CONTINUOUS = -1


def schedule_window_seq(event: dict, ts):
    """Which scoring window of a recurring-schedule event (web82a) contains
    ``ts``: the window's 0-based sequence, :data:`CONTINUOUS` for an event
    with no schedule, or ``None`` when the event is scheduled but no window
    is open at ``ts`` (scoring frozen). ``[start, end)`` containment, matching
    services/event_schedule.in_any_window."""
    windows = event.get("windows")
    if not windows:
        return CONTINUOUS
    for seq, (ws, we) in enumerate(windows):
        if ws <= ts < we:
            return seq
    return None


def schedule_open(event: dict, ts) -> bool:
    """Whether ``ts`` falls inside one of the event's scoring windows
    (continuous events are always open). See :func:`schedule_window_seq`."""
    return schedule_window_seq(event, ts) is not None


def window_scope(seq) -> str:
    """Redis key suffix isolating per-window absolute-counter state (KC
    watermarks, XP baselines) on a recurring-schedule event.

    Every window re-baselines: kills and XP earned while scoring was CLOSED
    must not fold into the next window's first credit. Both sources of
    absolute counters — plugin ``kill_count`` and WOM hiscores — carry the
    same suffix, so the double-count guard between them still holds inside a
    window. Empty for continuous events, which keep their historical keys."""
    return "" if seq is None or seq == CONTINUOUS else f":w{seq}"


def _task_wom_metrics(task_type, npcs) -> dict:
    """``{wom metric slug -> normalized NPC name}`` for a kc_target's (or a
    botw competition's) NPC set (NPCs without a WOM hiscores metric are
    absent — those stay plugin-only)."""
    if task_type not in ("kc_target", "competition") or not npcs:
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


def _enrich_matcher_precompute(d: dict) -> dict:
    """Attach the per-type lookups ``match_task`` reads off a task dict.

    Split out of :func:`_task_to_dict` because competition bonus rules embed a
    task config VERBATIM and are matched by running the very same
    :func:`match_task` over a synthetic dict built from it — the whole reason
    one rule type buys the entire task-builder vocabulary. Every key here is a
    pure function of ``type`` + ``config``, which is what makes a synthetic
    dict a first-class citizen rather than a lookalike.

    ``resolve_wom`` is deliberately NOT passed for metric paths here: WOM
    metrics drive the reconciler's hiscores planning, and on a competition the
    RACE owns that. A bonus rule must never widen it.
    """
    ttype = d.get("type")
    config = d.get("config") or {}
    if ttype == "item_collection":
        d["item_source_index"] = _item_source_index(config)
        d["task_source_npcs"] = _norm_npc_set(config.get("source_npcs"))
        d["pet_name_set"] = _config_pet_names(config)
        d["metric_paths"] = list(_metric_paths_from_config(config, resolve_wom=False))
    elif ttype == "ca_target":
        # Normalized once per state load; the matcher compares against it.
        d["ca_name_set"] = {_norm(n) for n in (config.get("task_names") or ()) if _norm(n)}
    return d


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
    if task.type == "ca_target":
        _enrich_matcher_precompute(d)
    if task.type == "competition":
        # Precompute the sotw/botw matcher snapshot (metric + bonus-rule
        # lookups) as plain data, plus the kc-watermark inputs a botw shares
        # with kc_target (``kc_npcs`` scopes the per-NPC absolute-KC state,
        # ``wom_metrics`` lets the WOM reconciler plan hiscores KC). Guarded:
        # the unit-test conftest stubs services.competition.
        try:
            from services.competition import CompetitionConfig
            d["competition"] = CompetitionConfig(d["config"]).matcher_index()
        except Exception:
            d["competition"] = {}
        d["kc_npcs"] = list((d["competition"] or {}).get("npcs") or [])
        d["wom_metrics"] = _task_wom_metrics(task.type, d["kc_npcs"])
        # Turn each embedded bonus-rule config into a real matcher task dict.
        # It borrows the competition task's own id so anything that reads
        # ``task["id"]`` off a match (state scoping, the ledger insert) still
        # points at the one hidden race task the whole event folds from.
        for rule in ((d["competition"] or {}).get("task_rules") or ()):
            embedded = rule.get("task")
            if not isinstance(embedded, dict):
                continue
            embedded["id"] = d["id"]
            embedded["event_id"] = d["event_id"]
            embedded.setdefault("points", 0)
            embedded.setdefault("requires_confirmation", d["requires_confirmation"])
            embedded["config"] = parse_task_config(embedded.get("config"))
            _enrich_matcher_precompute(embedded)
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

    # Recurring schedules (web82a): attach each scheduled event's compiled
    # scoring windows. Windows are static data (rule edits rewrite the rows
    # and bump the matcher), so the per-envelope gate needs no Redis — plain
    # containment against this snapshot.
    scheduled_ids = [e.id for e in events if getattr(e, "schedule_config", None)]
    if scheduled_ids:
        from db.models import EventWindow

        for w in (session.query(EventWindow)
                  .filter(EventWindow.event_id.in_(scheduled_ids))
                  .order_by(EventWindow.event_id, EventWindow.starts_at)
                  .all()):
            state.events[w.event_id].setdefault("windows", []).append(
                (w.starts_at, w.ends_at))

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

    # Bingo EHB: the NPCs each event's tasks make relevant, plus the freeze
    # gate (which tasks are already done per team). Both are Redis-cached, so
    # this stays a cache read on the overwhelming majority of reloads.
    if _EFFORT_ENABLED:
        from utils.redis import redis_client

        redis_conn = getattr(redis_client, "client", None)
        for event_id in state.events:
            state.effort_npcs_by_event[event_id] = load_effort_npcs(
                session, redis_conn, event_id,
                state.tasks_by_event.get(event_id) or [])
        teams_by_event: dict = {}
        for team in teams:
            teams_by_event.setdefault(team_event[team.id], []).append(team.id)
        state.completed_tasks_by_team = _refresh_done_tasks(
            session, redis_conn, event_ids, teams_by_event)
    return state


def _refresh_done_tasks(session, redis_conn, event_ids, teams_by_event) -> dict:
    """Rebuild the per-team completed-task sets backing the effort freeze.

    ``EventProgress`` is the truth; Redis is a cache the hot path can read
    without a query. Rebuilding wholesale each reload is what makes a revoke
    (which clears ``completed``) un-freeze effort automatically, and what heals
    a lost write-through.

    Returns ``{(event_id, team_id): {task_id, ...}}``.
    """
    if not event_ids:
        return {}
    from db.models import EventProgress

    try:
        rows = (session.query(EventProgress.event_id, EventProgress.team_id,
                              EventProgress.task_id)
                .filter(EventProgress.event_id.in_(event_ids),
                        EventProgress.completed.is_(True))
                .all())
    except Exception:
        return {}
    done: dict = {}
    for event_id, team_id, task_id in rows:
        if team_id is None:
            continue
        done.setdefault((event_id, team_id), set()).add(int(task_id))
    if redis_conn is None:
        return done
    try:
        pipe = redis_conn.pipeline()
        # Every team is rewritten, not just the ones with completions: a team
        # whose only completion was revoked must end up with an EMPTY set, and
        # skipping it would leave the stale members frozen forever.
        for event_id, team_ids in teams_by_event.items():
            for team_id in team_ids:
                key = _done_tasks_key(event_id, team_id)
                task_ids = done.get((event_id, team_id))
                pipe.delete(key)
                if task_ids:
                    pipe.sadd(key, *task_ids)
                    pipe.expire(key, _STATE_KEY_TTL)
        pipe.execute()
    except Exception:
        pass
    return done


def reconcile_effort_freezes(session, state: "MatcherState") -> int:
    """Stamp / clear ``web_event_effort.frozen_at`` to match the freeze gate.

    ``frozen_at`` is a reporting flag ("this boss stopped counting"), so it is
    maintained off the submission path, on the same cadence as matcher state.
    Accrual itself stops the instant a task completes (the Redis write-through
    in ``apply_ledger_row``); this only catches the display up, and reverses
    itself when a revoke re-opens a task. Returns the number of rows changed.
    Caller owns the commit.
    """
    if not _EFFORT_ENABLED or not state.effort_npcs_by_event:
        return 0
    from db.models import EventEffort

    changed = 0
    now = datetime.now()
    for event_id, npcs in state.effort_npcs_by_event.items():
        if not npcs:
            continue
        # (team_id -> frozen npc ids) for every team that has completed
        # anything; teams with no completions can freeze nothing.
        frozen_by_team: dict = {}
        for (ev, team_id), done in state.completed_tasks_by_team.items():
            if ev != event_id:
                continue
            ids = {
                int(entry["npc_id"]) for entry in npcs.values()
                if entry.get("npc_id")
                and entry.get("tasks")
                and all(int(t) in done for t in entry["tasks"])
            }
            if ids:
                frozen_by_team[team_id] = ids
        try:
            rows = (session.query(EventEffort)
                    .filter(EventEffort.event_id == event_id).all())
        except Exception:
            continue
        for row in rows:
            should_freeze = int(row.npc_id) in frozen_by_team.get(row.team_id, ())
            if should_freeze and row.frozen_at is None:
                row.frozen_at = now
                changed += 1
            elif not should_freeze and row.frozen_at is not None:
                row.frozen_at = None
                changed += 1
    return changed


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


def _window_start_for(event: dict, seq):
    """The opening instant of the scoring window a submission landed in — the
    sub-window's start on a recurring-schedule event (web82a), else the
    event's overall window start."""
    if seq is not None and seq != CONTINUOUS:
        windows = event.get("windows") or ()
        if 0 <= seq < len(windows):
            return windows[seq][0]
    return event.get("window_start")


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


#: The two sources of ABSOLUTE kill counts folded into one scope. They are
#: tracked separately because they do not always count the same event — see
#: :func:`_fold_kc_watermark`.
KC_SOURCE_PLUGIN = "plugin"   # RuneLite loot-tracker kill_count, on a drop
KC_SOURCE_WOM = "wom"         # WOM boss metric, via the event reconciler
_KC_SOURCES = (KC_SOURCE_PLUGIN, KC_SOURCE_WOM)

#: A single fold crediting more than this many kills is far more likely to be
#: a counter mismatch, a KC reset or an account re-link than a real grind.
#: LOGGED, never clamped: a month-long event's first WOM sync for a member who
#: joined at the window start legitimately lands in the hundreds, and silently
#: eating that would be a worse bug than the one being watched for.
_KC_DELTA_ALERT = 250


def _kc_source_base_key(event_id: int, task_id, player_id: int, source: str) -> str:
    """One source's FIXED baseline — the absolute KC it first reported for this
    scope. Deliberately a different prefix from the legacy ``kcbase`` key so
    the two schemes can coexist through a mid-event deploy."""
    return f"events:{event_id}:kcsrc:{task_id}:{player_id}:{source}"


def _kc_source_gain_key(event_id: int, task_id, player_id: int, source: str) -> str:
    """One source's monotone estimate of the scope's in-event kills."""
    return f"events:{event_id}:kcgain:{task_id}:{player_id}:{source}"


def _kc_credited_key(event_id: int, task_id, player_id: int) -> str:
    """Kills this scope has already handed out, across all sources."""
    return f"events:{event_id}:kccred:{task_id}:{player_id}"


def _redis_int(redis_conn, key, default: int = 0) -> int:
    """``GET key`` as an int, tolerating bytes / missing / junk values."""
    try:
        raw = redis_conn.get(key)
    except Exception:
        return default
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


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


def _legacy_kc_max(redis_conn, event_id: int, task_id, player_id: int) -> int:
    """Highest absolute KC an EARLIER scheme already credited for this scope.

    Two of them now: the single shared watermark this function's predecessor
    kept at ``kcbase`` (see :func:`_fold_kc_watermark`), and the older
    ``kcdedupe`` member set before that. Used only as a floor on a source's
    first baseline after a mid-event deploy, so kills counted under the old
    scheme are not handed out a second time.
    """
    return max(
        _legacy_kcdedupe_max(redis_conn, event_id, task_id, player_id),
        _redis_int(redis_conn, f"events:{event_id}:kcbase:{task_id}:{player_id}"),
    )


def _kc_estimate(redis_conn, event_id: int, task_id, player_id: int,
                 exclude: Optional[str] = None) -> int:
    """The scope's best in-event kill estimate: the largest any single source
    supports. A MAX, never a sum — two sources counting the same kills must
    not both credit."""
    return max((_redis_int(redis_conn, _kc_source_gain_key(
        event_id, task_id, player_id, src))
        for src in _KC_SOURCES if src != exclude), default=0)


def _kc_new_baseline(redis_conn, event_id: int, task_id, player_id: int,
                     source: str, kc_abs: int, seed, first_credit_offset: int) -> int:
    """The baseline to bank the first time ``source`` reports for this scope.

    A *virtual* baseline: the source's estimate is always ``kc_abs - base``, so
    ``base`` encodes both where the source started counting and what it
    inherited. It may legitimately be negative (an inherited estimate larger
    than the counter itself) and must not be clamped to 0.
    """
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = None
    if seed is not None and 0 < seed <= kc_abs:
        # A WOM window-start seed already spans the whole window, so it needs
        # no inheritance — it IS the complete measure.
        base = seed
    else:
        prior = _kc_estimate(redis_conn, event_id, task_id, player_id, exclude=source)
        if prior > 0:
            # A source that starts reporting mid-event missed everything before
            # its first report. It inherits the scope's current estimate so it
            # can keep the scope LIVE from here (plugin drops land in seconds;
            # the WOM reconciler is activity-tiered and can lag hours) without
            # re-crediting anything. Nothing else is credited right now: the
            # inherited estimate is exactly what has already been paid out.
            return kc_abs - prior
        base = max(kc_abs - int(first_credit_offset), 0)
    # Mid-event deploy from an earlier scheme: its credits are not repeated.
    # Only meaningful for the scope's FIRST source (a later one inherits the
    # estimate above instead), and capped at ``kc_abs`` so a floor left by the
    # other source's larger scale cannot mute this one forever.
    return min(max(base, _legacy_kc_max(redis_conn, event_id, task_id, player_id)),
               kc_abs)


def _fold_kc_watermark(redis_conn, event_id: int, task_id, player_id: int,
                       kc_abs, *, seed=None, first_credit_offset: int = 0,
                       source: str = KC_SOURCE_PLUGIN,
                       staged: Optional[StagedWrites] = None) -> int:
    """Return the kills an absolute-KC report newly earns for this scope.

    ``task_id`` is the bare task id, a per-NPC scope for multi-NPC kc tasks
    (:func:`_kc_state_scope`), or an ``eff:`` effort scope.

    **Absolutes are never subtracted across sources.** Two sources report an
    absolute KC for the same scope — a plugin drop's loot-tracker
    ``kill_count`` and WOM's boss metric — and they do not always count the
    same event. Fortis Colosseum is the proof: WOM's ``sol_heredit`` counts
    COMPLETED runs while the plugin's chest KC counts every attempt, so one
    player's two counters read 172 and 920 for the same account on the same
    day. The single shared watermark this replaces folded whichever source
    arrived second against the other's absolute value and credited the
    LIFETIME DIFFERENCE as in-event kills — 748 of them inside 100 minutes
    (bug report #131).

    So each source keeps its OWN baseline (``kcsrc``, set by
    :func:`_kc_new_baseline`) and its own monotone estimate of the scope's
    in-event kills (``kcgain``, always ``kc_abs - base``). The scope credits
    ``max(estimate)`` less what it has already paid out (``kccred``). Where
    the two counters agree — every ordinary boss — max-of-estimates behaves
    exactly like the old "whichever is ahead wins, the other folds to 0"
    double-count guard. Where they disagree, neither can inflate the other,
    and the scope still tracks whichever source has seen more (so a player who
    stops running the plugin keeps earning from WOM, and one who starts
    mid-event keeps the scope updating live between WOM syncs).

    Credits already granted through the no-kill_count cooldown fallback
    (``kcfallback``) are subtracted from any positive delta so they never
    double-count.

    With ``staged`` the state advance + fallback consume are deferred to
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
    src = str(source or KC_SOURCE_PLUGIN)
    base_key = _kc_source_base_key(event_id, task_id, player_id, src)
    gain_key = _kc_source_gain_key(event_id, task_id, player_id, src)
    cred_key = _kc_credited_key(event_id, task_id, player_id)
    try:
        raw_base = redis_conn.get(base_key)
        if raw_base is None:
            base = _kc_new_baseline(redis_conn, event_id, task_id, player_id,
                                    src, kc_abs, seed, first_credit_offset)
            _write("set", base_key, base, ex=_STATE_KEY_TTL)
        else:
            base = int(raw_base)

        # This source's estimate: monotone, so a stale or regressed report can
        # never walk it back.
        estimate = max(0, kc_abs - base, _redis_int(redis_conn, gain_key))
        _write("set", gain_key, estimate, ex=_STATE_KEY_TTL)

        earned = max(estimate, _kc_estimate(
            redis_conn, event_id, task_id, player_id, exclude=src))
        credited = _redis_int(redis_conn, cred_key)
        delta = earned - credited
        if delta <= 0:
            return 0
        # Everything up to ``earned`` is accounted for once this returns —
        # part by this delta, part by the fallback credits consumed below.
        _write("set", cred_key, earned, ex=_STATE_KEY_TTL)
        if delta > _KC_DELTA_ALERT:
            import logging

            logging.getLogger(__name__).warning(
                "large KC fold: event=%s scope=%s player=%s source=%s "
                "kc=%s base=%s delta=%s — verify the counter's semantics",
                event_id, task_id, player_id, src, kc_abs, base, delta)
        fb_key = _kc_fallback_key(event_id, task_id, player_id)
        pending = _redis_int(redis_conn, fb_key)
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


# ══════════════════════════════════════════════════════════════════════════════
# Effort (Bingo EHB) — non-crediting activity at an event's relevant NPCs
# ══════════════════════════════════════════════════════════════════════════════
# Contribution counters only record credit, so a player who grinds a boss all
# week for a tile and never gets the drop is invisible. Effort records the
# kills anyway, priced into EHB by ``services/event_effort.py``. It never
# touches the ledger, the progress rollup, points or the team score.
#
# The relevance map is DB-backed (item -> source NPC inference), so it is
# resolved once per task-set and cached in Redis: matcher state reloads every
# 30s and must stay a query burst, not a burst plus per-item wiki lookups.
_EFFORT_MAP_TTL = int(os.getenv("EVENT_EFFORT_MAP_TTL", str(6 * 3600)))
_EFFORT_ENABLED = (os.getenv("EVENT_EFFORT_ENABLED", "true").strip().lower()
                   not in ("0", "false", "no"))
# Relevance is driven by TASK TYPE, not event kind: a board-game event's tiles
# are ordinary kc/pb/item tasks and a player grinding one is participating just
# as much as a bingo player. (An earlier event-kind allowlist excluded
# board_game, which read as "ToB KC earns nothing here".) Kinds with no
# effort-bearing task types — loot_sweep, whose tasks accrue loot value rather
# than being worked toward — resolve to an empty map on their own.


def _effort_map_key(event_id: int) -> str:
    return f"events:{event_id}:effortnpcs"


def _done_tasks_key(event_id: int, team_id) -> str:
    """Task ids already complete for one team — the freeze gate. Mirrors
    ``EventProgress.completed``; refreshed wholesale on every state load, and
    written through on completion so a freeze takes effect immediately rather
    than at the next reload."""
    return f"events:{event_id}:donetasks:{team_id}"


# Bump when the RESOLVER changes what a given task set maps to (a new task type
# joins the relevance union, the caps move, …). The digest below covers task
# config only, so without this a logic change would keep serving stale cached
# maps until someone happened to edit a task — which is exactly how pb_target
# support first landed invisibly.
_EFFORT_MAP_VERSION = "4"


def _effort_tasks_digest(tasks) -> str:
    """Identity of an event's task set for effort purposes. Changes only when
    a task's type/target/source config changes (or the resolver version does),
    so editing an unrelated field (label, points) does not force re-inference."""
    parts = [f"v{_EFFORT_MAP_VERSION}"]
    for task in sorted(tasks, key=lambda t: t.get("id") or 0):
        config = task.get("config") or {}
        parts.append("|".join((
            str(task.get("id")), str(task.get("type")), _norm(task.get("target")),
            json.dumps({k: config.get(k) for k in
                        ("npcs", "source_npcs", "item_npcs", "items", "any_of",
                         "groups", "paths", "pets")}, sort_keys=True, default=str),
        )))
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]


def _npc_ids_for_names(session, names) -> dict:
    """``{normalized npc name: npc_id}`` for explicitly-named NPCs. Effort rows
    are keyed by npc_id, so a name we cannot resolve is simply not tracked."""
    wanted = [n for n in {_norm(n) for n in names} if n]
    if not wanted:
        return {}
    from db.models import NpcList

    out = {}
    for npc_id, npc_name in session.query(NpcList.npc_id, NpcList.npc_name).all():
        key = _norm(npc_name)
        if key in wanted and key not in out:
            out[key] = int(npc_id)
    return out


def _loot_sweep_effort_names(config: dict) -> tuple:
    """``([normalized npc names], [item names])`` an EHE-relevant loot_sweep
    task points at.

    Reads the raw config rather than :class:`services.loot_sweep.LootSweepConfig`
    so this stays import-free (the unit-test conftest stubs ``services``), and
    because the group-level structure is what matters here — the normalized
    matcher index flattens item aliases, which would inflate the inference list
    with duplicates. Handles the v1 back-compat shape (flat top-level
    ``items``/``npcs``) the same way the sweep scorer does.
    """
    groups = list(config.get("groups") or [])
    if not groups and config.get("items"):
        groups = [{"npcs": config.get("npcs") or [], "items": config.get("items")}]

    npcs: set = set()
    item_names: set = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        allowed = _norm_npc_set(group.get("npcs"))
        if allowed:
            npcs |= set(allowed)
            continue
        for it in (group.get("items") or []):
            name = it if isinstance(it, str) else (it or {}).get("item_name") or (it or {}).get("name")
            name = _norm(name)
            if name:
                item_names.add(name)
    return sorted(npcs), sorted(item_names)


def _effort_item_names(task: dict) -> set:
    """Item/pet names an unrestricted collection task should infer NPCs from.

    Wider than :func:`_config_item_entries`, which only sees the config's item
    lists, and that gap was invisible: a SINGLE-item task stores its item in
    the ``target`` COLUMN with no config list at all (the matcher reads it at
    :func:`item_match_quantity`), so "obtain 7 Sarachnis cudgels" inferred no
    sources and Sarachnis earned no effort while multi-item tiles on the same
    board worked fine (bug report #131). ``config.pets`` — how the task form
    stores a customized pet-category list — had the same hole.
    """
    config = task.get("config") or {}
    names = set(_config_item_entries(config))
    for pet in (config.get("pets") or ()):
        name = _norm(pet if isinstance(pet, str)
                     else (pet or {}).get("pet_name") or (pet or {}).get("name"))
        if name:
            names.add(name)
    target = _norm(task.get("target"))
    if target:
        names.add(target)
    return names


def _effort_task_descriptors(session, tasks) -> list:
    """Per-task ``{task_id, npcs, npc_ids, item_names}`` for
    :func:`services.event_effort.build_effort_map`.

    Config parsing stays here (one source of truth); the effort module only
    unions and caps. An item task that already carries a source restriction
    contributes those NPCs explicitly and skips inference — the admin has
    already answered the question inference exists to guess at.
    """
    descriptors, explicit = [], set()
    for task in tasks:
        ttype = task.get("type")
        npcs, item_names = [], []
        if ttype == "kc_target":
            npcs = list(_kc_npcs(task))
        elif ttype == "pb_target":
            # Every attempt at the boss is work toward the task, whether or not
            # the run beat the target time — a 40-minute ToB that missed the
            # cutoff is still 40 minutes spent on this event.
            npcs = [n for n in (_norm(task.get("target")),) if n]
        elif ttype in ("item_collection", "pet_collection", "loot_value"):
            config = task.get("config") or {}
            configured = set(_norm_npc_set(config.get("source_npcs")))
            for allowed in (_item_source_index(config) or {}).values():
                configured |= set(allowed)
            # any_path KC paths name their own NPCs — they are explicit too.
            for path in _metric_paths(task):
                configured |= set(path.get("npcs") or ())
            if ttype == "loot_value":
                # ``target`` scopes a loot_value task to ONE NPC (see the
                # matcher) — an NPC name, not an item, so it is explicit and
                # never fed to item inference.
                configured |= {n for n in (_norm(task.get("target")),) if n}
            if configured:
                npcs = sorted(configured)
            else:
                item_names = sorted(_effort_item_names(task))
        elif ttype == "loot_sweep":
            # A loot-sweep set names its own sources per group ("Barrows" for
            # the brothers' pieces), so those are explicit; groups that name
            # none fall back to inferring from their item list, exactly like an
            # unrestricted item task. Both can be non-empty on one task — a
            # sweep mixes NPC-scoped and open groups freely.
            npcs, item_names = _loot_sweep_effort_names(task.get("config") or {})
        if not npcs and not item_names:
            continue
        explicit.update(npcs)
        descriptors.append({
            "task_id": task.get("id"),
            "npcs": npcs,
            "item_names": item_names,
        })
    ids = _npc_ids_for_names(session, explicit) if explicit else {}
    for d in descriptors:
        d["npc_ids"] = ids
    return descriptors


def load_effort_npcs(session, redis_conn, event_id: int, tasks) -> dict:
    """``{normalized npc name: {npc_id, metric, tasks}}`` for one event.

    Cached in Redis under the task-set digest so the 30s state reload is free
    unless the tasks actually changed. Any failure degrades to an empty map:
    effort disappears from the UI, nothing else breaks.
    """
    if not _EFFORT_ENABLED or not tasks:
        return {}
    digest = _effort_tasks_digest(tasks)
    key = _effort_map_key(event_id)
    try:
        raw = redis_conn.get(key) if redis_conn is not None else None
        if raw:
            cached = json.loads(raw)
            if cached.get("digest") == digest:
                return cached.get("npcs") or {}
    except Exception:
        pass

    try:
        from db.item_sources import source_npcs_for_item_names
        from services.event_effort import EFFORT_SOURCES_PER_ITEM, build_effort_map
        from utils.wiseoldman import wom_boss_metric

        descriptors = _effort_task_descriptors(session, tasks)
        npcs = build_effort_map(
            descriptors,
            resolve_sources=lambda names: source_npcs_for_item_names(
                session, names, per_item_limit=EFFORT_SOURCES_PER_ITEM),
            boss_metric=wom_boss_metric,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "effort map resolution failed for event %s", event_id)
        return {}

    try:
        if redis_conn is not None:
            redis_conn.setex(key, _EFFORT_MAP_TTL,
                             json.dumps({"digest": digest, "npcs": npcs}))
    except Exception:
        pass
    return npcs


def _effort_frozen(redis_conn, event_id: int, team_id, task_ids, done_cache: dict) -> bool:
    """True when every task this NPC feeds is already complete for the team.

    Brondt's requirement: a boss stops counting once the tile it feeds is done,
    or the post-completion farm inflates the score. ``done_cache`` memoizes the
    per-team set for the duration of one envelope.
    """
    if not task_ids:
        return False
    done = done_cache.get(team_id)
    if done is None:
        done = set()
        try:
            if redis_conn is not None:
                for raw in (redis_conn.smembers(
                        _done_tasks_key(event_id, team_id)) or ()):
                    try:
                        done.add(int(raw))
                    except (TypeError, ValueError):
                        continue
        except Exception:
            done = set()
        done_cache[team_id] = done
    return all(int(t) in done for t in task_ids if t is not None)


def mark_task_done(redis_conn, event_id: int, team_id, task_id) -> None:
    """Write-through of a completion into the freeze gate. Best-effort: the
    set is rebuilt from ``EventProgress`` on every state load, so a lost write
    only delays a freeze by one refresh interval."""
    if redis_conn is None or team_id is None or task_id is None:
        return
    try:
        key = _done_tasks_key(event_id, team_id)
        redis_conn.sadd(key, int(task_id))
        redis_conn.expire(key, _STATE_KEY_TTL)
    except Exception:
        pass


def record_effort(session, event_id: int, team_id, player_id: int, npc_norm: str,
                  entry: dict, delta: int, *, source: str, at: datetime,
                  completions: int = 0, rolls: int = 0) -> None:
    """Add ``delta`` kills (and ``completions`` completions, ``rolls`` rolls) to
    one (event, player, npc) effort row.

    Upsert rather than append: effort is a running counter per NPC, not a
    ledger. ``source`` records which side of the hybrid fold fed it — a row
    touched by both becomes ``both``, which is what tells the read model the
    freeze on that row is plugin-precise rather than WOM-lagged.

    ``completions`` is non-zero only at ``COMPLETION_MARKERS`` NPCs, where an
    attempt and a completion are different events and have to be priced
    separately. A call may carry completions with ``delta == 0`` (the plugin
    proving a completion for an attempt its chest KC already counted), so
    ``delta <= 0`` alone is not a reason to return.

    ``rolls`` is non-zero only at ``CLUE_TIERS`` NPCs, and always arrives with
    ``delta == 0``: a scroll is dealt by some *other* NPC's drop table, so the
    call that records it is never also recording a casket opening.
    """
    from db.models import EventEffort

    npc_id = entry.get("npc_id")
    delta = max(0, int(delta or 0))
    completions = max(0, int(completions or 0))
    rolls = max(0, int(rolls or 0))
    if not npc_id or (delta <= 0 and completions <= 0 and rolls <= 0):
        return
    row = (session.query(EventEffort)
           .filter(EventEffort.event_id == event_id,
                   EventEffort.player_id == player_id,
                   EventEffort.npc_id == npc_id)
           .first())
    if row is None:
        session.add(EventEffort(
            event_id=event_id, team_id=team_id, player_id=player_id,
            npc_id=npc_id, boss_metric=entry.get("metric"),
            kills=int(delta), completions=int(completions), rolls=int(rolls),
            source=source, first_at=at, last_at=at,
        ))
        return
    row.kills = int(row.kills or 0) + int(delta)
    if completions:
        row.completions = int(row.completions or 0) + int(completions)
    if rolls:
        row.rolls = int(row.rolls or 0) + int(rolls)
    row.last_at = at
    if team_id is not None:
        row.team_id = team_id
    if row.boss_metric is None and entry.get("metric"):
        row.boss_metric = entry["metric"]
    if row.source and row.source != source:
        row.source = "both"


#: Sanity clamp on one receipt's quantity. Scroll boxes arrive one at a time
#: (one row in 3,703 carried a 2 over a week of prod drops); anything larger is
#: a malformed envelope, not a player who was dealt 40 clues at once.
_CLUE_ROLL_MAX_QUANTITY = 5


def _apply_clue_roll(session, redis_conn, event: dict, npcs: dict, team_id,
                     player_id: int, envelope: dict, submitted_at: datetime,
                     done_cache: dict,
                     staged: Optional[StagedWrites] = None) -> None:
    """Credit a clue ROLL when a drop hands the player a scroll for a tier the
    event's tasks care about. Never raises.

    This is the half of suggestion #156 that could not be read off an existing
    counter. Caskets already reach effort as ordinary kills — the plugin
    reports one under the pseudo-NPC ``Clue Scroll (Tier)`` with the player's
    absolute completion count — but a casket can be *banked*, so openings alone
    say nothing about time spent inside the window. The scroll's arrival is
    what dates the clue, and it arrives at a different NPC entirely.

    Relevance and the freeze are the tier's, not the source boss's: a roll is
    credit toward the clue tile, so it stops when that tile does.
    """
    data = envelope.get("data") or {}
    clue_npc = _clue_roll_npc(data.get("item_name"))
    if clue_npc is None:
        return
    entry = npcs.get(clue_npc)
    if entry is None:
        return
    if _effort_frozen(redis_conn, event["id"], team_id, entry.get("tasks"),
                      done_cache):
        return
    try:
        qty = int(data.get("quantity") or 1)
    except (TypeError, ValueError):
        qty = 1
    qty = max(1, min(qty, _CLUE_ROLL_MAX_QUANTITY))
    try:
        # Deduped on the SOURCE kill (that boss's npc name + kill_count), so
        # neither a redelivered envelope nor the rest of that kill's loot can
        # re-credit the scroll. A skilling receipt (a clue bottle from fishing)
        # carries no kill count and falls to the same cooldown the crediting
        # paths use there.
        if not _kc_dedupe(redis_conn, event["id"], _roll_scope(clue_npc),
                          player_id, envelope, staged=staged):
            return
        record_effort(session, event["id"], team_id, player_id, clue_npc, entry,
                      0, source=KC_SOURCE_PLUGIN, at=submitted_at, rolls=qty)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "clue roll record failed (event %s, player %s, item %r)",
            event["id"], player_id, data.get("item_name"))


def _apply_effort(session, redis_conn, state: "MatcherState", event: dict,
                  team_id, player_id: int, envelope: dict, submitted_at: datetime,
                  done_cache: dict, staged: Optional[StagedWrites] = None) -> None:
    """Record effort for one envelope against one event. Never raises.

    Runs alongside — not inside — the task-match loop: the whole point is that
    it fires when nothing matched. Absolute KC is folded through the same
    :func:`_fold_kc_watermark` the crediting paths use, under a distinct
    ``eff:`` scope so the two can never consume each other's watermark.
    """
    if not _EFFORT_ENABLED:
        return
    npcs = state.effort_npcs_by_event.get(event["id"]) or {}
    if not npcs:
        return
    data = envelope.get("data") or {}
    kind = envelope.get("kind")

    if kind == "drop":
        # A drop can be BOTH a kill at its own NPC and a clue roll for a tier
        # (a Hellhound dropping a scroll box), so the roll is credited
        # alongside this path, never instead of it.
        _apply_clue_roll(session, redis_conn, event, npcs, team_id, player_id,
                         envelope, submitted_at, done_cache, staged=staged)
        npc_norm = _norm(data.get("npc_name"))
        kc_abs, seed = data.get("kill_count"), None
        source, offset = KC_SOURCE_PLUGIN, 1
    elif kind == "pb":
        # A kill-time submission is an attempt at the boss, and PB envelopes
        # carry no absolute KC — so this takes the same no-kill_count path the
        # crediting side uses: cooldown dedupe (which also collapses the drop
        # and the pb from ONE kill, since they share the scope and arrive
        # seconds apart) plus a fallback counter that a later absolute fold
        # subtracts. Without that counter, a boss that reports both would
        # double-count every kill.
        npc_norm = _norm(data.get("npc_name"))
        entry = npcs.get(npc_norm)
        if entry is None:
            return
        if _effort_frozen(redis_conn, event["id"], team_id, entry.get("tasks"), done_cache):
            return
        scope = _effort_scope(npc_norm)
        try:
            if not _kc_dedupe(redis_conn, event["id"], scope, player_id,
                              envelope, staged=staged):
                return
            fb_key = _kc_fallback_key(event["id"], scope, player_id)
            if staged is not None:
                staged.stage("incr", fb_key)
                staged.stage("expire", fb_key, _STATE_KEY_TTL)
            else:
                redis_conn.incr(fb_key)
                redis_conn.expire(fb_key, _STATE_KEY_TTL)
            record_effort(session, event["id"], team_id, player_id, npc_norm,
                          entry, 1, source="plugin", at=submitted_at)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "effort record failed (pb; event %s, player %s, npc %r)",
                event["id"], player_id, npc_norm)
        return
    elif kind == "wom_kc":
        metric = str(data.get("boss_metric") or "").strip().lower()
        npc_norm = next(
            (name for name, e in npcs.items() if e.get("metric") == metric), "")
        kc_abs, source, offset = data.get("kc"), KC_SOURCE_WOM, 0
        # Same D10 rule the crediting path uses: only seed from the window-start
        # value for players who were in before the window opened.
        seed = data.get("kc_start") if data.get("target_event_id") == event["id"] else None
    else:
        return

    entry = npcs.get(npc_norm)
    if entry is None:
        return
    if _effort_frozen(redis_conn, event["id"], team_id, entry.get("tasks"), done_cache):
        return
    marker = _effort_completion_marker(npc_norm)
    try:
        if marker is not None and source == KC_SOURCE_WOM:
            # At a marker NPC, WOM's metric counts COMPLETIONS while the
            # plugin's chest KC counts attempts. Folding it into the attempts
            # scope is exactly the unit confusion behind bug report #131, so it
            # gets its own scope and its own column — and credits NO kills,
            # because the plugin's chest KC already counted that attempt. The
            # read model takes max(kills, completions), which keeps a WOM-only
            # player (kills 0) whole.
            delta = _fold_kc_watermark(
                redis_conn, event["id"], _completion_scope(npc_norm), player_id,
                kc_abs, seed=seed, first_credit_offset=offset, source=source,
                staged=staged)
            if delta > 0:
                record_effort(session, event["id"], team_id, player_id, npc_norm,
                              entry, 0, source=source, at=submitted_at,
                              completions=delta)
            return
        delta = _fold_kc_watermark(
            redis_conn, event["id"], _effort_scope(npc_norm), player_id, kc_abs,
            seed=seed, first_credit_offset=offset, source=source, staged=staged)
        completions = 0
        if marker is not None and kind == "drop" and _is_completion_drop(
                npc_norm, data.get("item_name")):
            # The marker item proves this attempt reached the WOM-counted
            # event. Deduped per (npc, kill_count) on the completion scope, so
            # the other items from the same chest cannot re-credit it. The
            # fallback counter is the same one the pb path uses: a later
            # absolute WOM fold on this scope subtracts what we credited here
            # rather than counting the completion twice.
            comp_scope = _completion_scope(npc_norm)
            if _kc_dedupe(redis_conn, event["id"], comp_scope, player_id,
                          envelope, staged=staged):
                fb_key = _kc_fallback_key(event["id"], comp_scope, player_id)
                if staged is not None:
                    staged.stage("incr", fb_key)
                    staged.stage("expire", fb_key, _STATE_KEY_TTL)
                else:
                    redis_conn.incr(fb_key)
                    redis_conn.expire(fb_key, _STATE_KEY_TTL)
                completions = 1
        if delta > 0 or completions:
            record_effort(session, event["id"], team_id, player_id, npc_norm,
                          entry, delta, source=source, at=submitted_at,
                          completions=completions)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "effort record failed (event %s, player %s, npc %r)",
            event["id"], player_id, npc_norm)


def _effort_scope(npc_norm: str) -> str:
    from services.event_effort import effort_scope

    return effort_scope(npc_norm)


def _completion_scope(npc_norm: str) -> str:
    from services.event_effort import completion_scope

    return completion_scope(npc_norm)


def _effort_completion_marker(npc_norm: str):
    from services.event_effort import completion_marker

    return completion_marker(npc_norm)


def _is_completion_drop(npc_norm: str, item_name) -> bool:
    from services.event_effort import is_completion_drop

    return is_completion_drop(npc_norm, item_name)


def _roll_scope(npc_norm: str) -> str:
    from services.event_effort import roll_scope

    return roll_scope(npc_norm)


def _clue_roll_npc(item_name):
    from services.event_effort import clue_roll_npc

    return clue_roll_npc(item_name)


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

    message_config = effective_message_config(event.get("message_config"))
    wanted = should_send_event_message(message_config, notification_type)
    if (not wanted and notification_type == "event_task_progress"
            and data.get("progress_notify") in ("milestones", "all")):
        # A per-task progress_notify override outranks the event-level MODE
        # (which defaults to 'off'); only the toggle itself still mutes.
        wanted = bool(message_config["toggles"].get("event_task_progress", True))
    if not wanted:
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


def reconcile_bingo_bonuses(session, event, *, announce_lead: bool = True) -> dict:
    """Re-derive every team's bonus set from the current board state.

    Used after a *live* board replace (implicit lifecycle lets a never-
    scheduled active event edit its board): unwinds bonuses for lines the new
    board no longer holds and awards ones it already satisfies (e.g. all-free
    lines). Idempotent. ``event`` is the ORM row or an engine event dict.
    Returns {team_id: {"revoked": [...], "awarded": [...]}} for teams that
    changed.

    ``announce_lead=False`` for callers that bracket a WIDER batch of score
    writes with their own :func:`_leader_snapshot` (recompute_task_rollups) —
    two nested compares over the same hand-over would announce it twice.
    """
    from db.models import EventTeam

    ev = event if isinstance(event, dict) else _event_to_dict(event)
    summary: dict = {}
    if not ev.get("has_bingo"):
        return summary
    lead_before = _leader_snapshot(session, ev) if announce_lead else _NO_LEAD_SNAPSHOT
    for team in session.query(EventTeam).filter(EventTeam.event_id == ev["id"]).all():
        revoked = _unwind_bonuses(session, ev, team.id)
        awarded = [a["note"] for a in evaluate_bingo_bonuses(session, ev, team.id)]
        if revoked or awarded:
            summary[team.id] = {"revoked": revoked, "awarded": awarded}
    if summary:
        session.flush()
        _announce_lead_change(session, ev, lead_before, reason="board_edit")
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

    The in-game plugin inbox is fanned out here, before the Discord verbosity
    gates — the plugin has its own client-side progress toggle, and the HUD
    stays current off the /event_state refresh. By DEFAULT only 25/50/75%
    milestone crossings fan out (per-increment envelopes spammed teammates);
    a per-task ``config.progress_notify`` of 'all' opts one tile back into
    every increment — except continuous-metric tasks (xp_target/loot_value),
    which even then step at {PLUGIN_PROGRESS_STEP_PCT}% of the target so a
    10M-XP task pings its teams ~10 times total instead of per kill.

    The override ('off'/'milestones'/'all') replaces the event AND team modes
    for BOTH the plugin fan-out and the Discord enqueue — it's how one tile
    announces at 25/50/75% while another announces every item. The
    event-level ``event_task_progress`` toggle stays the Discord master mute;
    the plugin's client toggle stays its own. The override rides in the
    payload (``progress_notify``) so the sender's per-destination verbosity
    re-checks honor it too.
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
    task_mode = task_progress_notify_mode(task)
    if task_mode:
        base_payload["progress_notify"] = task_mode
    if task_mode == "off":
        send_plugin = False
    elif task_mode == "all":
        # Explicit per-tile opt-in to every increment — except meter tasks,
        # where every XP drop / loot stack is an increment and per-increment
        # envelopes would spam every teammate's chat.
        send_plugin = (
            _plugin_progress_step_crossed(previous, current, target_threshold)
            if task.get("type") in PLUGIN_PROGRESS_STEP_TASK_TYPES else True)
    else:
        # 'milestones' and INHERIT: 25/50/75% crossings only. Default changed
        # from every-increment 2026-08-18 (owner: don't spam by default) — the
        # HUD stays current off the /event_state refresh either way.
        send_plugin = bool(crossed_pcts)
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
    if task_mode:
        # Tile-level decision is authoritative for Discord too; only the
        # event-level toggle (the master mute) still silences it.
        if not config["toggles"].get("event_task_progress", True):
            return
        effective = task_mode
    else:
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


#: :func:`_leader_snapshot` sentinel — "nothing to compare later, skip". Kept
#: distinct from ``None``, which is a real snapshot meaning "no sole leader".
_NO_LEAD_SNAPSHOT = object()

#: Channel kinds an ``event_lead_change`` can land in (KIND_FOR_TYPE plus its
#: announcements fallback) — see :func:`_warn_unroutable_lead_change`.
_LEAD_CHANGE_KINDS = ("leaderboard", "announcements")


def _leader_snapshot(session, event: dict):
    """Who leads BEFORE a batch of score writes, for :func:`_announce_lead_change`.

    The snapshot/compare pair has to bracket EVERY score write one apply
    performs, not just the first. Bracketing the base task points alone
    announced tile-driven overtakes and silently missed every bingo
    line/blackout overtake (t60: "only the first tile and the final lead change
    before the blackout") — those bonuses score after that window closed.
    """
    if event.get("id") is None:
        return _NO_LEAD_SNAPSHOT
    return _current_leader(session, event["id"], strict=True)


def _lead_changes_announceable(event_row) -> bool:
    """Whether an ADMIN-driven score correction on this event should announce.

    Live/draft: yes — a revoke or a task edit that changes who is winning is
    exactly what the leaderboard channel is for. Past: no — the standings are
    settled, the wrap-up has posted, and retro cleanups (see
    ``scripts/dedupe_multipath_drops.py``) would resurrect a finished event's
    channel weeks later. The scoring paths don't consult this: a completion
    that announces is announcing its own message anyway.
    """
    return (getattr(event_row, "status", None) or "active") != "past"


def _team_representative_player(session, team_id):
    """Any roster member of ``team_id``. ``notification_queue.player_id`` is NOT
    NULL and admin-driven score changes have no acting player, so one is
    borrowed from the team that took the lead rather than dropping the row (the
    in-game fan-out resolves its own audience from the roster regardless)."""
    if team_id is None:
        return None
    try:
        from db.models import EventTeamMember

        row = (session.query(EventTeamMember.player_id)
               .filter(EventTeamMember.team_id == team_id)
               .order_by(EventTeamMember.player_id.asc())
               .first())
        return row[0] if row else None
    except Exception:
        return None


def _warn_unroutable_lead_change(session, event: dict) -> None:
    """Log a lead change that has nowhere to post.

    ``event_lead_change`` targets the 'leaderboard' channel and falls back to
    'announcements'; with neither configured (and no per-team channel wanting
    it) the sender parks the row as ``skipped`` while completions keep posting
    to 'completions' — which reads to organisers as "lead changes don't fire".
    """
    try:
        from db.models import EventChannel

        routed = (session.query(EventChannel.id)
                  .filter(EventChannel.event_id == event["id"],
                          EventChannel.kind.in_(_LEAD_CHANGE_KINDS),
                          EventChannel.channel_id.isnot(None))
                  .first())
        if routed:
            return
        from services.event_team_discord import team_channel_interest

        if team_channel_interest(session, event["id"], "event_lead_change"):
            return
        import logging

        logging.getLogger(__name__).warning(
            "event_lead_change for event %s (%s) has no leaderboard/announcements "
            "channel configured — the message will be skipped",
            event["id"], event.get("name"))
    except Exception:
        pass  # unit-test stubs / transient read — never break an apply


def _claim_lead_change(event_id, team_id) -> bool:
    """Take cross-lane ownership of announcing ``team_id``'s hand-over.

    The apply lanes run concurrently (``workers/event_consumer.LANES``, sharded
    by player) on READ COMMITTED sessions, so a lane's post-apply standings read
    sees the OTHER lanes' *committed* score writes. On a busy event several
    lanes therefore observe the same hand-over and each would enqueue its own
    ``event_lead_change`` — naming the right team but stamped with its own,
    unrelated completion. Nothing downstream dedupes them: the
    notification_queue unique index cannot collide across different
    player/task/``at`` payloads.

    The ``SET .. GET`` swap is the compare-and-set: exactly one caller gets back
    a value other than ``team_id`` — the one that moved the watermark — and only
    that caller announces.

    Every degraded path announces rather than going silent (a duplicate is
    recoverable; missed announcements are the bug t60 fixed): no Redis handle, a
    Redis error, and a missing key (fresh event, worker restart, TTL) all return
    True, the miss re-seeding the watermark with the team that just took the
    lead. The caller has already established from its own before/after snapshot
    that the lead moved, so that is not a spurious announcement. The claim is
    not transactional: an apply that rolls back after claiming skips one.
    """
    try:
        from utils.redis import redis_client

        conn = getattr(redis_client, "client", None)
        if conn is None:
            return True
        previous = conn.set(LEAD_WATERMARK_KEY.format(event_id=int(event_id)),
                            str(int(team_id)), ex=LEAD_WATERMARK_TTL_SECONDS,
                            get=True)
    except Exception:
        return True
    if isinstance(previous, bytes):
        previous = previous.decode("utf-8", "replace")
    return previous is None or str(previous) != str(int(team_id))


def _announce_lead_change(session, event: dict, previous_leader, player_id=None,
                          *, reason: str = "score",
                          extra: Optional[dict] = None) -> Optional[dict]:
    """Enqueue ``event_lead_change`` when the sole leader changed hands since
    ``previous_leader`` (a :func:`_leader_snapshot`). Exactly one comparison per
    batch of score writes, made after the LAST of them — never mid-apply.

    Tie semantics are deliberate and inherited from ``_current_leader(strict=
    True)``: a shared top score has no sole leader, so *tying* the lead
    announces nothing (nobody took it) and the move that BREAKS the tie
    announces its winner. Dropping from sole first place into a tie is silent
    for the same reason — the standings message carries the detail.

    The new leader need not be the team whose score just moved: a revoke hands
    the lead to somebody else without touching their row. The snapshot compare
    is therefore only the *local* half of the test — it says "the lead looks
    different from where this transaction started", which under concurrent
    lanes is also true for lanes that merely READ somebody else's hand-over.
    :func:`_claim_lead_change` is the half that decides who announces it.
    """
    if previous_leader is _NO_LEAD_SNAPSHOT:
        return None
    session.flush()
    new_leader = _current_leader(session, event["id"], strict=True)
    if new_leader is None:
        return None
    if previous_leader is not None and previous_leader[0] == new_leader[0]:
        return None
    team_id, team_score = new_leader
    if not _claim_lead_change(event["id"], team_id):
        return None
    if player_id is None:
        player_id = _team_representative_player(session, team_id)
    payload = {
        "team_id": team_id,
        # int, not the 2dp float _current_leader returns: the plugin types
        # EventNotification.Data.teamScore as Integer and gson's nextInt()
        # throws on a fractional value, discarding the whole already-drained
        # /notifications batch — and loot_sweep scores are fractional. Neither
        # lead-change renderer prints it (event_message_layouts' lead-change
        # tokens are team_name/task_label/lead_via_line, and the legacy embed
        # in event_notifications shows standings), so 2dp buys nothing here.
        "team_score": int(round(team_score)),
        "previous_team_id": previous_leader[0] if previous_leader else None,
        # NOT "reason": that token already means "why a lifecycle step failed"
        # in services/event_message_layouts, and the token map is shared by
        # every notification type.
        "lead_reason": reason,
        # notification_queue is uniquely indexed on (type, player, group, data)
        # and _enqueue_notification swallows the collision, so a byte-identical
        # payload inside the prune window is dropped silently — the same team
        # retaking the lead at the same score (revoke → re-award, or a see-saw
        # between two fixed scores) never posted a second time. team_score, the
        # caller's ledger row id and this stamp make every genuine hand-over its
        # own row. Not rendered; discriminator only.
        "at": datetime.now().isoformat(timespec="microseconds"),
    }
    if extra:
        payload.update(extra)
    _warn_unroutable_lead_change(session, event)
    _enqueue_notification(session, "event_lead_change", event, player_id, payload)
    return payload


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
    # Leader before this receipt scores; compared once at the end of the apply
    # (_announce_lead_change) instead of around the write itself.
    lead_before = _NO_LEAD_SNAPSHOT
    if delta and team_id is not None:
        lead_before = _leader_snapshot(session, event)
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = round(float(team.score or 0) + delta, 2)
            team_score = team.score
            session.flush()

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

    # Enrich the lead-change with the receipt that triggered it (item + points),
    # so the message can name the drop that took the lead.
    _announce_lead_change(session, event, lead_before, player_id,
                          reason="completion", extra={
                              "task_id": task["id"],
                              "task_label": task.get("label"),
                              "received_item": completion.matched_target,
                              "points": delta,
                              "completion_id": completion.id,
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


def _competition_applied_rows(session, task: dict, team_id) -> list:
    """Applied ledger rows for one competition (task, team) — the recompute
    input for its standings (same status set as the other continuous kinds)."""
    from db.models import EventCompletion

    return list(
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.status.in_(APPLIED_BONUS_STATUSES))
        .all()
    )


def _competition_fold(session, task: dict, team_id, *, include=None,
                      exclude_id=None) -> tuple:
    """``(per_player, config)`` for a competition (task, team), optionally
    folding an unsaved ``include`` row / dropping ``exclude_id`` — the same
    before/after trick as :func:`_loot_sweep_score`."""
    from services.competition import CompetitionConfig, fold_rows

    rows = _competition_applied_rows(session, task, team_id)
    if exclude_id is not None:
        rows = [r for r in rows if r.id != exclude_id]
    if include is not None and all(r.id != include.id for r in rows):
        rows = rows + [include]
    config = CompetitionConfig(task.get("config") or {})
    return fold_rows(rows, config), config


def _competition_rank(per_player: dict, config, player_id) -> tuple:
    """``(rank, participants, leader)`` under the event's ranking mode —
    leader is ``(player_id, value)`` (ties break to the lower id, stable)."""
    from services.competition import rank_value

    ordered = sorted(
        ((rank_value(entry, config), pid) for pid, entry in per_player.items()),
        key=lambda t: (-t[0], t[1]))
    rank = None
    for i, (_value, pid) in enumerate(ordered):
        if pid == player_id:
            rank = i + 1
            break
    leader = (ordered[0][1], ordered[0][0]) if ordered else None
    return rank, len(ordered), leader


def _announce_milestone_crossings(session, event: dict, task: dict, config,
                                  completion, prev_entry: dict, entry: dict,
                                  player_name, rank, participants,
                                  bonus_delta: int) -> None:
    """One ``event_competition_bonus`` message per milestone rule this row
    pushed a player past. Same payload contract as a real bonus award, so no
    layout or token work is needed to render it."""
    from services.competition import (bonus_detail, player_points, rank_value,
                                      score_text)

    prev_bonus = prev_entry.get("bonus") or {}
    for rule_id, slot in (entry.get("bonus") or {}).items():
        if slot.get("type") != "milestone":
            continue
        before = int((prev_bonus.get(rule_id) or {}).get("awarded") or 0)
        gained_now = int(slot.get("awarded") or 0)
        if gained_now <= before:
            continue
        rule = config.rules_by_id.get(rule_id)
        earned = (gained_now - before) * (rule.points if rule else 0)
        detail = bonus_detail(rule_id, config, gained_now, points=earned)
        _enqueue_notification(session, "event_competition_bonus", event,
                              completion.player_id, {
                                  "task_id": task["id"],
                                  "task_label": task.get("label"),
                                  "team_id": completion.team_id,
                                  "player_id": completion.player_id,
                                  "player_name": player_name,
                                  "competition": True, "points_based": True,
                                  "points": earned,
                                  "bonus": detail,
                                  "rank": rank, "participants": participants,
                                  "gained": entry.get("gained", 0),
                                  "bonus_points": entry.get("bonus_points", 0),
                                  "total_points": player_points(entry, config),
                                  "rank_value_text": score_text(
                                      rank_value(entry, config), config),
                                  "ranking_mode": config.ranking_mode,
                                  "metric_kind": config.metric_kind,
                                  "matched_target": None,
                                  "received_item": None,
                                  "source_type": completion.source_type,
                                  "proof_url": None,
                              })


def _apply_competition(session, redis_conn, event: dict, task: dict, completion,
                       player_name: Optional[str] = None) -> dict:
    """Apply one competition ledger row (a gained delta OR a bonus award):
    recompute the standings fold off the ledger, refresh the roster team's
    running totals, and emit side effects. Competition tasks never "complete"
    — ``EventProgress.progress`` is the team's total GAINED (metric units)
    and ``EventTeam.score`` the total under the event's ranking mode, both
    pure functions of the applied ledger like loot_sweep's."""
    from db.models import EventProgress, EventTeam
    from services.competition import (bonus_detail, parse_bonus_note,
                                      player_points, team_totals)

    team_id = completion.team_id
    player_id = completion.player_id

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

    prev_fold, config = _competition_fold(session, task, team_id,
                                          exclude_id=completion.id)
    curr_fold, _ = _competition_fold(session, task, team_id, include=completion)
    prev_gained, prev_score = team_totals(prev_fold, config)
    curr_gained, curr_score = team_totals(curr_fold, config)
    progress.progress = curr_gained
    progress.completed = False

    team_score = None
    score_delta = curr_score - prev_score
    if score_delta and team_id is not None:
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = int(float(team.score or 0)) + score_delta
            team_score = team.score
            session.flush()
    # Contribution points (EventPlayerPoints) are deliberately NOT rewritten
    # per row here — on an XP-snapshot stream that O(roster) delete-and-
    # rewrite would run thousands of times a day. finalize_competition
    # (services/event_lifecycle.py) writes them once, at the end.

    entry = curr_fold.get(player_id) or {"gained": 0, "bonus_points": 0, "bonus": {}}
    prev_entry = prev_fold.get(player_id) or {"gained": 0, "bonus_points": 0, "bonus": {}}
    rank, participants, leader = _competition_rank(curr_fold, config, player_id)
    parsed_bonus = parse_bonus_note(completion.note)
    quantity = max(int(completion.quantity or 1), 1)
    # What this row was actually WORTH. Never the row's quantity for a bonus:
    # a ``task`` rule's quantity is credit units, and it pays nothing at all
    # until the rule's need is met (then the whole award at once). Reading it
    # off the fold is correct under every rule dialect, and is already what
    # EventTeam.score deltas on.
    bonus_delta = (int(entry.get("bonus_points") or 0)
                   - int(prev_entry.get("bonus_points") or 0))
    row_value = bonus_delta if parsed_bonus is not None else quantity

    result = {
        "kind": "competition",
        "event_id": event["id"],
        "task_id": task["id"],
        "team_id": team_id,
        "player_id": player_id,
        "delta": row_value,
        "is_bonus": parsed_bonus is not None,
        "gained": entry.get("gained", 0),
        "bonus_points": entry.get("bonus_points", 0),
        "points": player_points(entry, config),
        "rank": rank,
        "participants": participants,
        "progress": curr_gained,
    }
    if team_score is not None:
        result["team_score"] = team_score
    if leader is not None:
        result["leader"] = {"player_id": leader[0], "value": leader[1]}
    session.flush()

    frame = dict(result)
    if player_name:
        frame["player_name"] = player_name
    frame["task_label"] = task.get("label")
    _publish(event["id"], frame)

    # Milestone rules ("every 100 kills = 10 pts") pay off the gained total and
    # write NO ledger row of their own, so the crossing can only be seen by
    # diffing the folds — which is exactly what makes them impossible to
    # double-count. A gained row may cross several steps at once (a WOM top-up
    # can be worth thousands of kills); that is one message, for the total.
    if parsed_bonus is None and team_id is not None and bonus_delta > 0:
        _announce_milestone_crossings(session, event, task, config, completion,
                                      prev_entry, entry, player_name,
                                      rank, participants, bonus_delta)

    # Bonus awards are the kind's announce-worthy moments (the gained stream
    # is continuous — a message per XP snapshot would flood every channel; the
    # live leaderboard message carries that story instead).
    if parsed_bonus is not None and team_id is not None:
        from services.competition import rank_value, score_text

        rule_type, rule_id = parsed_bonus
        awarded_n = (entry.get("bonus") or {}).get(rule_id, {}).get("awarded", 0)
        prev_awarded = (prev_entry.get("bonus") or {}).get(rule_id, {}).get("awarded", 0)
        # Announce a rule's AWARD, not every row it recorded. A "collect all
        # four uniques" or "500 points of loot" rule writes a row per drop;
        # only the one that finally earns the payout is a moment worth a
        # message (and a common-item pool would otherwise fire dozens of times
        # per player). Discrete rules award on every row, so this is a no-op
        # for pet/time_under.
        if awarded_n <= prev_awarded:
            return result
        time_text = None
        raw_note = str(completion.note or "")
        if "|" in raw_note:
            time_text = raw_note.split("|", 1)[1].strip() or None
        detail = bonus_detail(rule_id, config, awarded_n,
                              matched_target=completion.matched_target,
                              time_text=time_text, points=bonus_delta)
        payload = {
            "task_id": task["id"], "task_label": task.get("label"),
            "team_id": team_id, "player_id": player_id, "player_name": player_name,
            "competition": True, "points_based": True,
            "points": bonus_delta,
            "bonus": detail,
            "rank": rank, "participants": participants,
            "gained": entry.get("gained", 0),
            "bonus_points": entry.get("bonus_points", 0),
            "total_points": player_points(entry, config),
            # Pre-worded ranked value ("213 pts" / "2.48M XP") for the
            # position line — the layout context stays a pure pass-through.
            "rank_value_text": score_text(rank_value(entry, config), config),
            "ranking_mode": config.ranking_mode,
            "metric_kind": config.metric_kind,
            "matched_target": completion.matched_target,
            # Pet awards: the pet's item icon becomes the message thumbnail
            # (the sender's completion_icon resolution keys off received_item).
            # The award's item icon becomes the message thumbnail (the
            # sender's completion_icon resolution keys off received_item).
            # A task rule's matched_target is an item name too — a Tanzanite
            # fang award should show the fang, not nothing.
            "received_item": (completion.matched_target
                              if rule_type in ("pet", "task") else None),
            "source_type": completion.source_type,
            "proof_url": completion.proof_url,
        }
        _enqueue_notification(session, "event_competition_bonus", event,
                              player_id, payload)
        # In-game plugin inbox: "+N pts — <reason>". Best-effort; unit-test
        # stubs lack the plugin module.
        try:
            from services.plugin_notifications import (
                fan_out_event_notification, resolve_item_icon_id,
            )
            plugin_payload = {
                "task_id": task["id"], "task_label": task.get("label"),
                "team_id": team_id, "player_id": player_id,
                "player_name": player_name,
                "points": bonus_delta, "team_score": team_score,
                "progress": entry.get("gained", 0),
                "received_item": (completion.matched_target
                                  if rule_type in ("pet", "task") else None),
            }
            if rule_type in ("pet", "task") and completion.matched_target:
                icon = resolve_item_icon_id(session, completion.matched_target)
                if icon:
                    plugin_payload["icon_item_id"] = icon
            fan_out_event_notification(session, "event_task_progress", event,
                                       plugin_payload)
        except ImportError:
            pass
        except Exception as plugin_err:
            print(f"competition plugin fan-out failed: {plugin_err}")
    return result


def _revoke_competition(session, event: dict, task: dict, team_id, completion) -> dict:
    """Recompute a competition (task, team) after a row was revoked. The
    caller already flipped the row to ``revoked``, so the applied-ledger fold
    excludes it; per-player bonus caps self-heal (the fold counts surviving
    rows only — a freed slot pays out again on the next qualifying kill)."""
    from db.models import EventProgress, EventTeam
    from services.competition import team_totals

    # P0-7: locked read — progress is a read-modify-write shared between the
    # consumer and webapi confirm/revoke flows; unlocked, one side's update
    # silently overwrites the other's under REPEATABLE READ.
    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
                .with_for_update()
                .first())
    prev_fold, config = _competition_fold(session, task, team_id,
                                          include=completion)
    curr_fold, _ = _competition_fold(session, task, team_id)
    _prev_gained, prev_score = team_totals(prev_fold, config)
    curr_gained, curr_score = team_totals(curr_fold, config)

    if progress is None:
        if curr_gained <= 0 and curr_score <= 0:
            return {"progress": 0, "completed": False, "team_score": None}
        progress = EventProgress(event_id=event["id"], task_id=task["id"],
                                 team_id=team_id, progress=0, completed=False)
        session.add(progress)
    progress.progress = curr_gained
    progress.completed = False

    team_score = None
    score_delta = curr_score - prev_score
    if score_delta and team_id is not None:
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = int(float(team.score or 0)) + score_delta
            team_score = team.score

    session.flush()
    frame = {"kind": "revoke", "event_id": event["id"], "task_id": task["id"],
             "team_id": team_id, "progress": curr_gained, "competition": True,
             "player_id": completion.player_id}
    if team_score is not None:
        frame["team_score"] = team_score
    _publish(event["id"], frame)
    return {"progress": curr_gained, "completed": False, "team_score": team_score}


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
    # SOTW/BOTW race task: continuous per-player standings off the ledger.
    if _list_kind(task) == "competition":
        return _apply_competition(session, redis_conn, event, task, completion,
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
    # Effort freeze (Bingo EHB): the NPCs feeding this task stop accruing once
    # every task they feed is done for this team. Written through immediately
    # so the farm that continues right after a tile completes doesn't score;
    # the set is rebuilt from EventProgress on each state load, which is what
    # makes a later revoke un-freeze it.
    mark_task_done(redis_conn, event["id"], team_id, task["id"])

    team_score = None
    points = int(task.get("points") or 0)
    # Leader BEFORE any of this apply's score writes — the task's own points
    # here AND the bingo line/blackout bonuses _complete_bingo_cells awards
    # below. Compared once at the end (_announce_lead_change): comparing around
    # the write below alone announced tile overtakes and missed every
    # bonus-driven one (t60). A zero-point tile still snapshots on a bingo
    # event, where it can finish a line worth points; off the board it moves no
    # score at all, so it skips the standings reads entirely.
    lead_before = _NO_LEAD_SNAPSHOT
    if team_id is not None and (points or event.get("has_bingo")):
        lead_before = _leader_snapshot(session, event)
    if points and team_id is not None:
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            team.score = int(team.score or 0) + points
            team_score = team.score
            session.flush()
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
    # Manual awards carry the organizer's reason — surface it so the
    # completion message can say WHY credit was granted, not just "manual".
    _note = display_note(completion.note)
    if _note:
        notification["note"] = _note
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
    # After the task points AND any line/blackout bonus this completion earned.
    _announce_lead_change(session, event, lead_before, player_id,
                          reason="completion", extra={
                              "task_id": task["id"],
                              "task_label": task.get("label"),
                              "completion_id": completion.id,
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
    if kind == "competition":
        # Gained rows always advance the running totals. A bonus row is dead
        # weight once its player sits at the rule's per-player cap — and the
        # count is over SURVIVING rows, so revoking an award frees its slot.
        from services.competition import (CompetitionConfig, bonus_award_count,
                                          parse_bonus_note, rule_rows,
                                          task_rule_progress)
        parsed = parse_bonus_note(getattr(candidate, "note", None))
        if parsed is None:
            return True
        cfg = CompetitionConfig(task.get("config") or {})
        rule = cfg.rules_by_id.get(parsed[1])
        rows = _competition_applied_rows(session, task, team_id)
        if rule is not None and rule.type == "task":
            # A task rule's rows are PROGRESS, not awards, so the gate is
            # "did the number move?" — not "is there an award left?". The
            # third item of a five-item set earns nothing yet and must still
            # be recorded, or the set could never complete.
            held = rule_rows(rows, candidate.player_id, rule.id)
            return (task_rule_progress(held + [candidate], rule)
                    > task_rule_progress(held, rule))
        cap = rule.max_awards if rule is not None else 1
        return bonus_award_count(rows, candidate.player_id, parsed[1]) < cap
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
                      kind, matched_target, quantity, bonus=None):
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

    Competition ``task`` bonus rules need this just as much — an item rule
    would otherwise pay twice, once for the drop and once for the collection
    log slot it unlocks seconds later, and the two rows carry different guids
    so the unique index does not help. But every rule on a competition shares
    ONE task id, so the echo lookup is additionally scoped to the candidate's
    own rule tag: without that, rule 3's drop would silently suppress rule 5's
    clog for the same item.
    """
    ttype = task.get("type")
    if ttype not in ("item_collection", "competition") or not matched_target:
        return quantity
    if kind not in ("drop", "clog"):
        return quantity
    if ttype == "competition" and (bonus or {}).get("type") != "task":
        return quantity
    from db.models import EventCompletion

    other = "drop" if kind == "clog" else "clog"
    cutoff = datetime.now() - timedelta(seconds=CLOG_ECHO_WINDOW_SECONDS)
    query = (
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.player_id == player_id,
                EventCompletion.source_type == other,
                EventCompletion.status.in_(("auto", "confirmed", "manual", "pending")),
                EventCompletion.created_at >= cutoff)
    )
    if ttype == "competition":
        from services.competition import bonus_note
        tag = bonus_note("task", int((bonus or {}).get("rule_id") or 0))
        # EQUALITY, not a prefix: `bonus:task:1%` also matches rules 10-12, so
        # on an event with ten or more rules a low-numbered rule would inherit
        # its neighbours' echoes and suppress its own legitimate credit. A task
        # row's note has no ` | ` human half (only time_under writes one), so
        # the tag is the whole string.
        query = query.filter(EventCompletion.note == tag)
    echoes = query.all()
    matched = [r for r in echoes if _norm(r.matched_target) == _norm(matched_target)]
    if not matched:
        return quantity
    if kind == "clog":
        return None
    remaining = int(quantity or 0) - sum(
        max(int(r.quantity or 1), 1) for r in matched)
    return remaining if remaining > 0 else None


def _dedupe_vestige_chain(session, task: dict, team_id, player_id, kind,
                          matched_target, bonus=None) -> bool:
    """True when this vestige credit may record; False when the player's
    ring/vestige chain already credited it (item_collection only).

    The DT2 pity mechanic makes the 1-ring, 2-ring and vestige drops (plus
    the vestige's clog echo) sightings of the SAME vestige-in-progress,
    spread over days — so one (task, team, player, vestige) credits ONCE,
    with no time window (unlike the 10-minute drop↔clog echo). Trade-off: a
    second full vestige cycle by the same player inside one event won't
    auto-credit again (astronomically rare; admins can award manually —
    manual rows neither block nor are blocked here).

    Competition ``task`` bonus rules need this for the same reason they need
    the clog-echo dedupe: ``_ring_vestige_for_task`` rewrites each Gold ring
    drop to the vestige name for the synthetic embedded task too, so a
    three-drop pity chain would count as three items toward the rule. Scoped
    to the candidate's own rule tag — every rule on a competition shares one
    task id, so an unscoped lookup would let one rule's chain silence
    another's.
    """
    ttype = task.get("type")
    if ttype not in ("item_collection", "competition") or player_id is None:
        return True
    if ttype == "competition" and (bonus or {}).get("type") != "task":
        return True
    if kind not in ("drop", "clog"):
        return True
    target_norm = _norm(matched_target)
    if target_norm not in _VESTIGE_NORMS:
        return True
    from db.models import EventCompletion

    query = (
        session.query(EventCompletion)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.team_id == team_id,
                EventCompletion.player_id == player_id,
                EventCompletion.source_type.in_(("drop", "clog")),
                EventCompletion.status.in_(("auto", "confirmed", "manual", "pending")))
    )
    if ttype == "competition":
        from services.competition import bonus_note
        query = query.filter(EventCompletion.note == bonus_note(
            "task", int((bonus or {}).get("rule_id") or 0)))
    prior = query.all()
    return not any(
        _norm(r.matched_target) == target_norm
        and (r.source_type or "") in ("drop", "clog")
        for r in prior
    )


def record_match(session, redis_conn, event: dict, task: dict, team_id: int,
                 player_id: int, quantity: int, envelope: dict,
                 cells: Optional[list] = None,
                 matched_target: Optional[str] = None,
                 path_idx: Optional[int] = None,
                 bonus: Optional[dict] = None) -> Optional[dict]:
    """Insert the ledger row for a match (idempotent on
    (task, team, submission_guid)); apply effects unless it needs
    confirmation. Returns a result dict, or None on duplicate replay.

    ``path_idx`` marks a metric-path match (any_path v2): the row is tagged
    with the path (``note`` = ``path:<idx>``) so the percent rollup folds it
    into the right path, and its guid gets a ``#p<idx>`` suffix — one envelope
    may legitimately write the item row AND one row per qualifying metric
    path, which the bare (task, team, guid) unique index would block.

    ``bonus`` (``{"rule_id", "type"}``) marks a competition bonus award: the
    row is tagged ``note = bonus:{type}:{rule_id}`` so the fold reads its
    quantity as POINTS (services/competition.py), and its guid gets a
    ``#b<rule_id>`` suffix — one pb envelope may award several time tiers,
    each its own row (same mechanism as the path suffix).

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
                                 envelope.get("kind"), matched_target,
                                 bonus=bonus):
        return None
    quantity = _dedupe_clog_echo(session, task, team_id, player_id,
                                 envelope.get("kind"), matched_target, quantity,
                                 bonus=bonus)
    if quantity is None:
        return None

    data = envelope.get("data") or {}
    status = completion_status(event, task, envelope, path_idx)
    guid = envelope.get("guid")
    if guid and path_idx is not None:
        # Truncate the base BEFORE suffixing so a long guid (the WOM
        # reconciler's composite keys) can never shed the path marker.
        guid = f"{str(guid)[:60]}#p{int(path_idx)}"
    elif guid and bonus is not None:
        # Same truncate-before-suffix rule for bonus awards.
        guid = f"{str(guid)[:60]}#b{int(bonus.get('rule_id') or 0)}"
    note = None
    if path_idx is not None:
        note = _path_note(path_idx)
    elif bonus is not None:
        note = f"bonus:{bonus.get('type')}:{int(bonus.get('rule_id') or 0)}"
        if bonus.get("time_ms"):
            # The kill time rides in the note's human half (`` | 0:52.6``) so
            # the award message and the admin ledger can both show it — the
            # row must be self-describing (apply_completion replays from DB).
            from services.competition import format_time_ms
            note = f"{note} | {format_time_ms(bonus['time_ms'])}"
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
        note=note,
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
    effort_done_cache: dict = {}

    for event_id, team_id, joined_at in memberships:
        event = state.events.get(event_id)
        if event is None:
            continue
        # Hard stop on end: ``state`` can be up to a refresh-interval stale,
        # and after a PREMATURE manual end the window check below can't catch
        # it (ends_at is still in the future) — an ended event must not keep
        # scoring or notifying off the stale snapshot.
        if is_event_ended(redis_conn, event_id):
            continue
        # PRD D10: joined_at is the credit cutoff; window rules (A5) freeze
        # evaluation outside the active window.
        if joined_at is not None and submitted_at < joined_at:
            continue
        if event["window_start"] is not None and submitted_at < event["window_start"]:
            continue
        if event["window_end"] is not None and submitted_at > event["window_end"]:
            continue
        # Recurring schedules (web82a): inside the overall window, scoring is
        # only open during the event's materialized sub-windows (e.g. the
        # weekends of a weekends-only month). Judged on the submission's own
        # timestamp — same freeze rule as the overall window; effort (EHE)
        # recording below is behind this gate too. The sequence scopes every
        # absolute-counter baseline so each window starts fresh (see
        # window_scope).
        window_seq = schedule_window_seq(event, submitted_at)
        if window_seq is None:
            continue
        wscope = window_scope(window_seq)
        if not accepts_submission_source(event, envelope):
            continue

        # Bingo EHB: record the kill as effort regardless of whether anything
        # below matches — that is the entire point (a week at a boss with no
        # drop should still show). Deliberately outside the task loop (effort
        # is per NPC, not per task, so one in-game kill counter feeds it once
        # however many tiles that NPC serves) and ahead of the board-game
        # current-tile gate: a board-game player is participating in the event
        # while they wait on a roll, so their kills still count.
        _apply_effort(session, redis_conn, state, event, team_id, player_id,
                      envelope, submitted_at, effort_done_cache, staged=staged)
        # Effort-only envelopes exist solely to feed the pass above — the WOM
        # reconciler emits them for bosses relevant to a task but not tracked
        # BY one, so letting them reach the matcher could only ever credit
        # something the event never asked for.
        if (envelope.get("data") or {}).get("effort_only"):
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
                # only for players in before the window opened (PRD D10). On a
                # recurring-schedule event the boundary that matters is the
                # SCORING window the envelope lands in — the reconciler bounds
                # each fetch to one window, so kc_start/xp_start are that
                # window's opening values.
                wom_seed_ok = (data.get("source") == "wom"
                               and data.get("target_event_id") == event_id
                               and _seed_allowed(
                                   joined_at,
                                   _window_start_for(event, window_seq)))
                if match["mode"] == "kc":
                    # Multi-NPC kc tasks keep absolute-KC state PER NPC — each
                    # NPC's kill_count is its own counter, so one shared
                    # watermark would swallow the lower counts. Metric-path
                    # matches scope per (task, path, NPC) on top. The window
                    # suffix re-baselines every scoring window (web82a).
                    kc_scope = _match_kc_scope(
                        task, match, _norm(data.get("npc_name"))) + wscope
                    try:
                        kill_count = int(data.get("kill_count"))
                    except (TypeError, ValueError):
                        kill_count = None
                    if kill_count is not None and kill_count > 0:
                        quantity = _fold_kc_watermark(
                            redis_conn, event_id, kc_scope, player_id,
                            kill_count, first_credit_offset=1,
                            source=KC_SOURCE_PLUGIN, staged=staged)
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
                        task, match, _match_wom_npc(task, match, metric)) + wscope
                    quantity = _fold_kc_watermark(
                        redis_conn, event_id, kc_scope, player_id, data.get("kc"),
                        seed=data.get("kc_start") if wom_seed_ok else None,
                        source=KC_SOURCE_WOM, staged=staged)
                    if quantity <= 0:
                        continue
                elif match["mode"] == "xp":
                    if xp_delta is None:
                        skill = data.get("skill")
                        xp_delta = _fold_xp_baseline(
                            redis_conn, event_id, player_id,
                            (f"{skill}{wscope}" if skill else skill),
                            data.get("xp"),
                            seed=data.get("xp_start") if wom_seed_ok else None,
                            staged=staged)
                    if xp_delta <= 0:
                        continue
                    quantity = xp_delta
                outcome = record_match(
                    session, redis_conn, event, task, team_id, player_id,
                    quantity, envelope, cells=state.cells_by_task.get(task["id"]),
                    matched_target=match.get("matched_target"),
                    path_idx=match.get("path"),
                    bonus=match.get("bonus"))
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


def _derive_applied_progress(session, task: dict, team_id) -> float:
    """One (task, team) rollup value derived purely from the SURVIVING applied
    ledger rows (``auto``/``confirmed``/``manual``) + the task config — never
    from the stored ``EventProgress`` value. Shared by :func:`revoke_ledger_row`
    and :func:`recompute_task_rollups` so "what does the ledger say now?" has a
    single source of truth. Not for loot_sweep (its stateless fold lives in
    ``_loot_sweep_score``)."""
    from db.models import EventCompletion

    if _pb_distinct_players(task):
        return _distinct_player_progress(
            session, task, team_id, effective_threshold(session, task, team_id))
    if _list_kind(task) in ("all_of", "assembly"):
        # Distinct-item semantics (revoked rows are already excluded — their
        # status flipped before this recompute).
        return _distinct_item_progress(session, task, team_id)
    if _list_kind(task) == "groups":
        return _grouped_item_progress(session, task, team_id)
    if _list_kind(task) == "any_path":
        return _anypath_item_progress(session, task, team_id)
    survivors = (session.query(EventCompletion)
                 .filter(EventCompletion.task_id == task["id"],
                         EventCompletion.team_id == team_id,
                         EventCompletion.status.in_(("auto", "confirmed", "manual")))
                 .all())
    return sum(
        max(int(r.quantity or 1), 1) for r in survivors
        if (r.source_type or "") != "bonus"
    )


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
    # Taking points BACK hands the lead over just as much as scoring does, and
    # the team that inherits it is not the one whose row moved — so the
    # snapshot covers the whole revoke (points, bonus unwind) and the compare
    # is event-wide. Announced, not suppressed: an admin correction that
    # changes who is winning is exactly what the leaderboard channel is for.
    # Deliberate exception: an event that already ENDED keeps its correction
    # quiet — its standings are settled and its channel has moved on.
    lead_before = (_leader_snapshot(session, event)
                   if _lead_changes_announceable(event_row) else _NO_LEAD_SNAPSHOT)
    lead_extra = {"task_id": task["id"], "task_label": task.get("label"),
                  "completion_id": completion.id}

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
        _announce_lead_change(session, event, lead_before, completion.player_id,
                              reason="revoke", extra=lead_extra)
        return {"progress": None, "completed": None, "team_score": team_score,
                "revoked_bonuses": [completion.note]}

    if _list_kind(task) == "loot_sweep":
        summary = _revoke_loot_sweep(session, event, task, team_id, completion)
        _announce_lead_change(session, event, lead_before, completion.player_id,
                              reason="revoke", extra=lead_extra)
        return summary

    if _list_kind(task) == "competition":
        # Per-player standings re-fold; the single-team score follows. Team
        # lead-change announcements are meaningless with one roster team, so
        # none fire here (the SSE frame carries the corrected numbers).
        return _revoke_competition(session, event, task, team_id, completion)

    new_progress = _derive_applied_progress(session, task, team_id)

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
    # After the points unwind AND the bonus unwind above.
    _announce_lead_change(session, event, lead_before, completion.player_id,
                          reason="revoke", extra=lead_extra)
    return {"progress": new_progress, "completed": now_completed,
            "team_score": team_score, "revoked_bonuses": revoked_bonuses}


def recompute_task_rollups(session, event_row, task_row, *,
                           old_points: Optional[int] = None,
                           preserve_completed: bool = False) -> dict:
    """Re-fold every (task, team) rollup after a LIVE task edit (web68a).

    The caller has already mutated + flushed the task row (new target/config/
    points) and owns the commit. Per team, this re-derives progress from the
    surviving applied ledger rows against the NEW goal, flips ``completed`` in
    either direction, adjusts ``EventTeam.score`` under the same row locks the
    consumer uses, rewrites/deletes per-player contribution points, syncs
    bingo-cell completions, and finally reconciles line/blackout bonuses
    event-wide. Publishes one ``kind:"recompute"`` SSE frame per touched team.
    Returns ``{"teams": {team_id: {...}}, "bonuses": {...}}`` for the caller's
    audit row / API response.

    ``old_points``: the task's points value BEFORE the edit — when provided
    and a rollup STAYS completed, the team score absorbs the (new − old)
    delta so past awards match the new value.

    ``preserve_completed``: a rollup that is ALREADY completed stays completed
    even if the surviving ledger no longer reaches the threshold. For retro
    cleanups that delete bad ledger rows (see
    ``scripts/dedupe_multipath_drops.py``) this is the honest answer, not a
    fudge: ``record_match`` refuses to record anything once a (task, team)
    rollup completes, so every qualifying submission after that moment was
    silently discarded and is unrecoverable. Re-deriving from what survives
    would "un-complete" a task the team may well have finished on real drops we
    never wrote down — visibly revoking a finished task, its points, its bingo
    cell and any line bonus it fed. Progress, per-player contribution shares
    and the ledger are still corrected; only the completed flag is held. Not
    for live task edits, where the ledger IS the whole truth.

    Raises ``ValueError("forward_only")`` for kinds with no recomputable
    ledger: manual-only types (custom/ehp_target/ehb_target) and board-game
    events (progress is entangled with turn state — coins/rolls must never
    be retro-granted).

    Concurrency: the same ``with_for_update`` progress/team locks as
    ``apply_ledger_row``/``revoke_ledger_row``, iterated in ascending team
    order, so a concurrent consumer apply blocks until our commit and then
    increments on top of the new base. The worker matches with its OLD task
    snapshot until the caller's post-commit ``_bump`` lands (sub-second
    typical, 30 s worst) — a row matched under the old goal in that window
    is an ordinary applied row, individually revocable via the existing
    revoke flow.
    """
    from db.models import (EventBingoCell, EventBingoCompletion,
                           EventCompletion, EventPlayerPoints, EventProgress,
                           EventTeam)

    event = _event_to_dict(event_row)
    task = _task_to_dict(task_row)

    if task.get("type") in ("custom", "ehp_target", "ehb_target") or \
            (event.get("kind") or "standard") == "board_game":
        raise ValueError("forward_only")
    if task.get("type") == "competition":
        # The managed race task is never editable through the task routes and
        # its config locks at activation — there is no legal live edit to
        # recompute for, and a wrong refold would rewrite standings. Apply/
        # revoke (which re-fold from the ledger) remain the correction paths.
        raise ValueError("forward_only")

    applied = ("auto", "confirmed", "manual")
    team_ids = {
        tid for (tid,) in session.query(EventProgress.team_id)
        .filter(EventProgress.task_id == task["id"]).all()
    }
    team_ids |= {
        tid for (tid,) in session.query(EventCompletion.team_id)
        .filter(EventCompletion.task_id == task["id"],
                EventCompletion.status.in_(applied),
                EventCompletion.team_id.isnot(None))
        .distinct().all()
    }

    cell_ids = []
    if event.get("has_bingo"):
        cell_ids = [c.id for c in session.query(EventBingoCell)
                    .filter(EventBingoCell.task_id == task["id"]).all()]

    new_points = int(task.get("points") or 0)
    is_sweep = _list_kind(task) == "loot_sweep"
    teams_summary: dict = {}
    # One snapshot for the whole re-fold (every team's delta plus the bonus
    # reconcile below), compared once at the end — a live task edit that moves
    # several teams at once is still a single hand-over. Taken lazily, right
    # before the FIRST score write: a re-fold that moves no score (the common
    # case) issues no standings queries at all.
    lead_before = _NO_LEAD_SNAPSHOT
    announce_lead = _lead_changes_announceable(event_row)

    for team_id in sorted(t for t in team_ids if t is not None):
        # P0-7: locked read — same progress→team lock order as apply/revoke.
        progress = (session.query(EventProgress)
                    .filter(EventProgress.task_id == task["id"],
                            EventProgress.team_id == team_id)
                    .with_for_update()
                    .first())
        was_completed = bool(progress.completed) if progress is not None else False

        if is_sweep:
            # Stateless refold — Loot Sweep never "completes"; the running
            # total IS the score (mirror _revoke_loot_sweep's arithmetic).
            previous_total = (round(float(progress.progress or 0), 2)
                              if progress is not None else 0.0)
            new_total = _loot_sweep_score(session, task, team_id)["total"]
            delta = round(new_total - previous_total, 2)
            if progress is None:
                if new_total <= 0:
                    continue
                progress = EventProgress(event_id=event["id"], task_id=task["id"],
                                         team_id=team_id, progress=0, completed=False)
                session.add(progress)
            progress.progress = new_total
            progress.completed = False
            team_score = None
            if delta:
                if announce_lead and lead_before is _NO_LEAD_SNAPSHOT:
                    lead_before = _leader_snapshot(session, event)
                team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                        .with_for_update().first())  # P0-7: locked score RMW
                if team is not None:
                    team.score = round(float(team.score or 0) + delta, 2)
                    team_score = team.score
            _award_contribution_points(
                session, event, task, team_id,
                _task_contributors(session, task["id"], team_id), new_total)
            teams_summary[team_id] = {
                "progress": new_total, "completed": False,
                "was_completed": was_completed, "score_delta": delta,
                "team_score": team_score,
            }
            continue

        new_progress = _derive_applied_progress(session, task, team_id)
        threshold = effective_threshold(session, task, team_id)
        now_completed = new_progress >= threshold if new_progress > 0 else False
        if preserve_completed and was_completed:
            # Post-completion credit was never recorded, so the surviving
            # ledger cannot prove the task is still finished — and cannot
            # disprove it either. Hold the flag; score, bingo cells and bonuses
            # then stay untouched while progress/contributions are corrected.
            now_completed = True
        if progress is None:
            if new_progress <= 0:
                continue
            progress = EventProgress(event_id=event["id"], task_id=task["id"],
                                     team_id=team_id, progress=0, completed=False)
            session.add(progress)
        progress.progress = new_progress
        progress.completed = now_completed
        if now_completed and not was_completed:
            # Honest timeline: when the surviving ledger last advanced, not
            # "when an admin edited the goal".
            last = (session.query(EventCompletion.created_at)
                    .filter(EventCompletion.task_id == task["id"],
                            EventCompletion.team_id == team_id,
                            EventCompletion.status.in_(applied))
                    .order_by(EventCompletion.created_at.desc())
                    .first())
            progress.completed_at = (last[0] if last and last[0] else datetime.now())
        elif not now_completed:
            progress.completed_at = None

        score_delta = 0
        if was_completed != now_completed and new_points:
            score_delta += new_points if now_completed else -new_points
        elif (was_completed and now_completed and old_points is not None
              and int(old_points) != new_points):
            score_delta += new_points - int(old_points)
        team_score = None
        if score_delta:
            if announce_lead and lead_before is _NO_LEAD_SNAPSHOT:
                lead_before = _leader_snapshot(session, event)
            team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                    .with_for_update().first())  # P0-7: locked score RMW
            if team is not None:
                team.score = int(team.score or 0) + score_delta
                team_score = team.score

        # Per-player contribution points follow the completed flag (Task 18
        # semantics): complete → idempotent rewrite at the NEW points value;
        # no longer complete → rows deleted.
        if now_completed:
            _award_contribution_points(
                session, event, task, team_id,
                _task_contributors(session, task["id"], team_id), new_points)
        elif was_completed:
            (session.query(EventPlayerPoints)
             .filter(EventPlayerPoints.task_id == task["id"],
                     EventPlayerPoints.team_id == team_id)
             .delete(synchronize_session=False))

        # Bingo cells follow the completed flag; bonuses reconcile once,
        # event-wide, after the loop.
        if cell_ids:
            if was_completed and not now_completed:
                (session.query(EventBingoCompletion)
                 .filter(EventBingoCompletion.cell_id.in_(cell_ids),
                         EventBingoCompletion.team_id == team_id)
                 .delete(synchronize_session=False))
            elif now_completed and not was_completed:
                existing = {
                    row.cell_id
                    for row in session.query(EventBingoCompletion)
                    .filter(EventBingoCompletion.cell_id.in_(cell_ids),
                            EventBingoCompletion.team_id == team_id).all()
                }
                for cid in cell_ids:
                    if cid not in existing:
                        session.add(EventBingoCompletion(
                            cell_id=cid, team_id=team_id, player_id=None))

        teams_summary[team_id] = {
            "progress": new_progress, "target": threshold,
            "completed": now_completed, "was_completed": was_completed,
            "score_delta": score_delta, "team_score": team_score,
        }

    session.flush()
    # One event-wide bonus reconcile: unwinds lines the new state no longer
    # holds, awards ones it now satisfies; adjusts team scores itself. It only
    # runs its own lead compare when this event announces at all AND we have no
    # snapshot to bracket it with — two nested compares over the same hand-over
    # would announce it twice.
    bonuses = reconcile_bingo_bonuses(
        session, event_row,
        announce_lead=announce_lead and lead_before is _NO_LEAD_SNAPSHOT)

    # Publish AFTER the reconcile so each frame carries the team's final score.
    touched = set(teams_summary) | set(bonuses)
    if touched:
        scores = dict(
            session.query(EventTeam.id, EventTeam.score)
            .filter(EventTeam.id.in_(touched)).all()
        )
        for team_id in sorted(touched):
            entry = teams_summary.get(team_id, {})
            frame = {"kind": "recompute", "event_id": event["id"],
                     "task_id": task["id"], "team_id": team_id}
            if "progress" in entry:
                frame["progress"] = entry["progress"]
                frame["completed"] = entry["completed"]
            if "target" in entry:
                frame["target"] = entry["target"]
            if team_id in scores:
                frame["team_score"] = scores[team_id]
                if team_id in teams_summary:
                    teams_summary[team_id]["team_score"] = scores[team_id]
            b = bonuses.get(team_id)
            if b:
                frame["bonuses"] = b
            _publish(event["id"], frame)

    _announce_lead_change(session, event, lead_before, reason="task_edit",
                          extra={"task_id": task["id"],
                                 "task_label": task.get("label")})
    return {"teams": teams_summary, "bonuses": bonuses}
