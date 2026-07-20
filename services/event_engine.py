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
  qualifying drops (default 1).
- ``kc_target`` — drop from the target NPC; each qualifying kill counts once
  (deduped by ``(npc, kill_count)`` per player via Redis).
- ``pb_target`` — pb for the target boss with time ≤ target_value seconds;
  completes on first match.
- ``xp_target`` — xp gained in the target skill since the first report after
  join (per-player baseline in Redis ``events:{eid}:xpbase:{pid}:{skill}``).
- ``skill_target`` — experience report for the target skill with
  level ≥ target_value; completes on first match.
- ``pet_collection`` — pet submission (``kind == "pet"``). A ``target`` names
  one specific pet; otherwise ``config.categories`` (a list like
  ``["boss"]``) gates by pet category via :mod:`utils.osrs_pets`, and an
  absent/empty list means "any pet" (the default set, misc excluded).
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

from sqlalchemy.exc import IntegrityError

# ── Redis keys / channels ─────────────────────────────────────────────────────
QUEUE_KEY = "events:submissions"           # LPUSH by producers, BRPOP by worker
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


def completion_threshold(task: dict) -> int:
    """Progress value at which the task completes."""
    if task.get("type") in ("pb_target", "skill_target"):
        return 1
    try:
        return max(int(task.get("target_value") or 0), 1)
    except (TypeError, ValueError):
        return 1


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


def _anypath_progress_from_rows(rows, config: dict, threshold: int) -> int:
    """Pure core of :func:`_anypath_item_progress` (unit-testable).

    Paths differ in size, so the single rollup integer is the *percentage* of
    the closest-to-done path scaled to the threshold (validation pins
    target_value to 100). Floor rounding means the threshold is hit exactly
    when some path's own need is fully met, never one drop early.
    """
    best = 0
    for path in (config.get("paths") or []):
        if not isinstance(path, dict):
            continue
        need = sum(n for _mode, _names, n in _parse_requirement_groups(path))
        if need <= 0:
            continue
        got = _grouped_progress_from_rows(rows, path, need)
        best = max(best, (got * threshold) // need)
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
    threshold = completion_threshold(task)
    kind = _list_kind(task)
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
        if kind not in ("drop", "clog"):
            return None
        qty = data.get("quantity", 1) if kind == "drop" else 1
        credit = item_match_quantity(task, data.get("item_name"), qty)
        if credit is None:
            return None
        # The matched name rides along to the ledger row so all_of/assembly
        # progress can count DISTINCT items rather than folding quantities.
        return {"mode": "count", "quantity": credit,
                "matched_target": str(data.get("item_name") or "").strip()[:120] or None}

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
            metric = task.get("wom_metric")
            if not metric or metric != str(data.get("boss_metric") or "").strip().lower():
                return None
            return {"mode": "kc_abs", "quantity": 0}
        if kind != "drop":
            return None
        if _norm(data.get("npc_name")) != _norm(task.get("target")) or not task.get("target"):
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
        return {"mode": "first", "quantity": 1}

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


def _task_wom_metric(task_type, target) -> Optional[str]:
    """WOM metric slug for a kc_target's NPC target (None when WOM has no
    hiscores metric for it — that task stays plugin-only)."""
    if task_type != "kc_target" or not target:
        return None
    try:
        from utils.wiseoldman import wom_boss_metric
        return wom_boss_metric(target)
    except Exception:
        return None


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
        "wom_metric": _task_wom_metric(task.type, task.target),
        "difficulty": getattr(task, "difficulty", None),
    }
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

def _xp_baseline_key(event_id: int, player_id: int, skill: str) -> str:
    return f"events:{event_id}:xpbase:{player_id}:{_norm(skill)}"


def _fold_xp_baseline(redis_conn, event_id: int, player_id: int, skill, xp,
                      seed=None) -> int:
    """Return xp gained since the last stored baseline and advance it.

    The first report after join only sets the baseline (delta 0) — PRD D10:
    no retroactive credit. Exception: WOM reconciler envelopes may carry a
    ``seed`` (the player's XP at the event window start, per WOM's snapshot
    history) — when the baseline is unset, the first fold credits from the
    seed instead, so plugin-less players still earn their in-window gains.
    """
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
            redis_conn.set(key, xp, ex=_STATE_KEY_TTL)
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                return 0
            return xp - seed if 0 < seed < xp else 0
        prev = int(prev)
        if xp <= prev:
            return 0
        redis_conn.set(key, xp, ex=_STATE_KEY_TTL)
        return xp - prev
    except Exception:
        return 0


def _seed_allowed(joined_at, window_start) -> bool:
    """WOM window-start seeding is only honest for players who joined at/before
    the window start; late joiners keep the lazy first-report baseline."""
    if joined_at is None or window_start is None:
        return True
    return joined_at <= window_start


def _kc_fallback_key(event_id: int, task_id: int, player_id: int) -> str:
    return f"events:{event_id}:kcfallback:{task_id}:{player_id}"


def _legacy_kcdedupe_max(redis_conn, event_id: int, task_id: int, player_id: int) -> int:
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


def _fold_kc_watermark(redis_conn, event_id: int, task_id: int, player_id: int,
                       kc_abs, *, seed=None, first_credit_offset: int = 0) -> int:
    """Return kills gained since the stored absolute-KC watermark, advance it.

    One watermark per (event, task, player), advanced by BOTH sources of
    absolute KC — plugin drops' ``kill_count`` and WOM hiscores — so whichever
    is ahead wins and the other folds to 0 (the double-count guard).

    First observation: baseline = ``seed`` (WOM's window-start KC) when given,
    else ``kc_abs - first_credit_offset`` (offset 1 keeps a first plugin drop
    crediting +1, as before). Credits already granted through the
    no-kill_count cooldown fallback (``kcfallback`` counter) are subtracted
    from any positive delta so they never double-count.
    """
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
        redis_conn.set(key, max(kc_abs, base), ex=_STATE_KEY_TTL)
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
                    redis_conn.set(fb_key, pending - consumed, ex=_STATE_KEY_TTL)
                else:
                    redis_conn.delete(fb_key)
        return delta
    except Exception:
        return 0


# Without a usable kill count, stacks from one kill are only distinguishable
# from a new kill by time: one kill's stacks arrive within a couple of
# seconds, while re-killing any multi-stack NPC takes far longer.
KC_FALLBACK_COOLDOWN_SECONDS = 10


def _kc_dedupe(redis_conn, event_id: int, task_id: int, player_id: int, envelope: dict) -> bool:
    """True if this drop represents a not-yet-counted kill for a kc task.

    Keyed per (npc, kill_count) so multi-item drops from one kill count once.
    When kill_count is unusable (absent, or 0 = the plugin's "unavailable"
    marker), fall back to a per-(task, player) cooldown — the old guid
    fallback counted every stack of a multi-item kill as its own kill.
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
            added = redis_conn.sadd(key, member)
            redis_conn.expire(key, _STATE_KEY_TTL)
            return bool(added)
        ts = int(envelope.get("ts") or time.time())
        last_key = f"events:{event_id}:kclast:{task_id}:{player_id}"
        last = redis_conn.get(last_key)
        if last is not None and ts - int(last) < KC_FALLBACK_COOLDOWN_SECONDS:
            return False
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


def _team_completed_idxs(session, cells, team_id: int) -> set:
    """Board idxs the team has completed, given the event's cell rows."""
    from db.models import EventBingoCompletion

    if not cells:
        return set()
    done_cell_ids = {
        row.cell_id
        for row in session.query(EventBingoCompletion).filter(
            EventBingoCompletion.cell_id.in_([c.id for c in cells]),
            EventBingoCompletion.team_id == team_id).all()
    }
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

    cells = session.query(EventBingoCell).filter(
        EventBingoCell.event_id == event["id"]).all()
    size = _board_size_for(event, cells)
    if not size:
        return []
    done_idxs = _team_completed_idxs(session, cells, team_id)

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
            EventCompletion.status.in_(APPLIED_BONUS_STATUSES)).all()
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

    team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
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
                            matched_target: Optional[str] = None) -> None:
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

    target_threshold = completion_threshold(task)
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
    if strict and len(rows) > 1 and int(rows[0].score or 0) == int(rows[1].score or 0):
        return None
    return (rows[0].id, int(rows[0].score or 0))


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

    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
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
        team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
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

    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
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
        team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
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

    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
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
    if _list_kind(task) in ("all_of", "assembly"):
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
        "target": completion_threshold(task),
    }

    newly_completed = (not already_completed
                       and progress.progress >= completion_threshold(task))
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
                matched_target=completion.matched_target)
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
        team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
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
            "target": completion_threshold(task),
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


def _row_advances_progress(session, task: dict, team_id, candidate) -> bool:
    """Would this (unsaved) ledger row move the (task, team) rollup at all?

    Only item-list kinds with per-item/per-group needs can say "no": once a
    listed item is already satisfied, another copy of it is dead weight. For
    plain count/kc/xp folds every row advances progress until the threshold
    (the completed gate in :func:`record_match` handles the rest)."""
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
        helper = _anypath_item_progress
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


def record_match(session, redis_conn, event: dict, task: dict, team_id: int,
                 player_id: int, quantity: int, envelope: dict,
                 cells: Optional[list] = None,
                 matched_target: Optional[str] = None) -> Optional[dict]:
    """Insert the ledger row for a match (idempotent on
    (task, team, submission_guid)); apply effects unless it needs
    confirmation. Returns a result dict, or None on duplicate replay.

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

    quantity = _dedupe_clog_echo(session, task, team_id, player_id,
                                 envelope.get("kind"), matched_target, quantity)
    if quantity is None:
        return None

    data = envelope.get("data") or {}
    status = completion_status(event, task, envelope)
    guid = envelope.get("guid")
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
    )
    if not _row_advances_progress(session, task, team_id, completion):
        return None  # matched item already satisfied — contributes nothing
    try:
        with session.begin_nested():
            session.add(completion)
            session.flush()
    except IntegrityError:
        return None  # replay of the same submission — idempotent no-op

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


def handle_envelope(session, redis_conn, state: MatcherState, envelope: dict) -> list:
    """Evaluate one queue envelope against the matcher state. Returns the list
    of result dicts (empty when nothing matched). Caller commits."""
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
            match = match_task(task, envelope)
            if match is None:
                continue
            quantity = match["quantity"]
            data = envelope.get("data") or {}
            # WOM envelopes carry the window-start value; seeding from it is
            # only valid for the event whose window produced it, and only for
            # players who were in before the window opened (PRD D10).
            wom_seed_ok = (data.get("source") == "wom"
                           and data.get("target_event_id") == event_id
                           and _seed_allowed(joined_at, event["window_start"]))
            if match["mode"] == "kc":
                try:
                    kill_count = int(data.get("kill_count"))
                except (TypeError, ValueError):
                    kill_count = None
                if kill_count is not None and kill_count > 0:
                    quantity = _fold_kc_watermark(
                        redis_conn, event_id, task["id"], player_id,
                        kill_count, first_credit_offset=1)
                    if quantity <= 0:
                        continue
                else:
                    # No usable absolute KC: cooldown dedupe as before, and
                    # note the credit so a later absolute fold subtracts it.
                    if not _kc_dedupe(redis_conn, event_id, task["id"], player_id, envelope):
                        continue
                    try:
                        fb_key = _kc_fallback_key(event_id, task["id"], player_id)
                        redis_conn.incr(fb_key)
                        redis_conn.expire(fb_key, _STATE_KEY_TTL)
                    except Exception:
                        pass
            elif match["mode"] == "kc_abs":
                quantity = _fold_kc_watermark(
                    redis_conn, event_id, task["id"], player_id, data.get("kc"),
                    seed=data.get("kc_start") if wom_seed_ok else None)
                if quantity <= 0:
                    continue
            elif match["mode"] == "xp":
                if xp_delta is None:
                    xp_delta = _fold_xp_baseline(
                        redis_conn, event_id, player_id,
                        data.get("skill"), data.get("xp"),
                        seed=data.get("xp_start") if wom_seed_ok else None)
                if xp_delta <= 0:
                    continue
                quantity = xp_delta
            outcome = record_match(
                session, redis_conn, event, task, team_id, player_id,
                quantity, envelope, cells=state.cells_by_task.get(task["id"]),
                matched_target=match.get("matched_target"))
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
        team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
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
            team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
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

    if _list_kind(task) in ("all_of", "assembly"):
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

    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
                .first())
    was_completed = bool(progress.completed) if progress is not None else False
    threshold = completion_threshold(task)
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
        team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
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
            team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
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
