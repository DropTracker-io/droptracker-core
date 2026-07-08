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

    { "v": 1, "kind": "drop|pb|clog|ca|experience",
      "guid": "...", "player_id": 123, "player_name": "...",
      "ts": 1751600000, "data": { ...type-specific... } }

v1 evaluation semantics (task doc table):

- ``item_collection`` — drop/clog item name match (case-insensitive; config
  kinds ``any_of``/``all_of``/``point_collection``/``assembly`` best-effort:
  every listed item name matches; ``point_collection`` credits the item's
  point weight). Progress unit = quantity.
- ``kc_target`` — drop from the target NPC; each qualifying kill counts once
  (deduped by ``(npc, kill_count)`` per player via Redis).
- ``pb_target`` — pb for the target boss with time ≤ target_value seconds;
  completes on first match.
- ``xp_target`` — xp gained in the target skill since the first report after
  join (per-player baseline in Redis ``events:{eid}:xpbase:{pid}:{skill}``).
- ``skill_target`` — experience report for the target skill with
  level ≥ target_value; completes on first match.
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
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError

# ── Redis keys / channels ─────────────────────────────────────────────────────
QUEUE_KEY = "events:submissions"           # LPUSH by producers, BRPOP by worker
ACTIVE_EVENTS_KEY = "events:active"        # set of active event ids (gate)
ADMIN_BUMP_CHANNEL = "rt:event-admin"      # pubsub bump on event/task/roster mutations

_STATE_KEY_TTL = 60 * 60 * 24 * 60         # 60 days for xp-baseline / kc-dedupe keys

# Task types the engine can evaluate automatically (v1).
AUTO_TASK_TYPES = ("item_collection", "kc_target", "pb_target", "xp_target", "skill_target",
                   "loot_value")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Producer helper (intake hot path — tiny, guarded, never raises)
# ══════════════════════════════════════════════════════════════════════════════

def queue_submission(kind: str, player_id, guid, data: dict,
                     world_type: str = "main", player_name: str = None,
                     ts: int = None) -> None:
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
    config. Supports ``items: [{item_name|name, quantity?, points?}]`` and a
    bare ``any_of: [str, ...]`` list."""
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


def _list_kind(task: dict) -> Optional[str]:
    """Item-list config kind (any_of/all_of/point_collection/assembly), if any."""
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

    if task_type == "kc_target":
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

    # ehp_target / ehb_target / custom: manual-confirmation only (Task 18).
    return None


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
        "has_bingo": bool(event.has_bingo),
        "board_size": int(event.board_size or 5),
        "bonus_line_points": int(event.bonus_line_points or 0),
        "bonus_blackout_points": int(event.bonus_blackout_points or 0),
        "window_start": max(starts) if starts else None,
        "window_end": min(ends) if ends else None,
    }


def _task_to_dict(task) -> dict:
    return {
        "id": task.id,
        "event_id": task.event_id,
        "type": task.type,
        "label": task.label,
        "target": task.target,
        "target_value": task.target_value,
        "points": int(task.points or 0),
        "requires_confirmation": bool(task.requires_confirmation),
        "config": parse_task_config(task.config),
    }


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
    return state


def set_active_events(redis_conn, event_ids) -> None:
    """Maintain the ``events:active`` gate set (worker lifecycle duty)."""
    try:
        pipe = redis_conn.pipeline()
        pipe.delete(ACTIVE_EVENTS_KEY)
        ids = [int(i) for i in (event_ids or [])]
        if ids:
            pipe.sadd(ACTIVE_EVENTS_KEY, *ids)
        pipe.execute()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# 3. Apply layer (worker + Task 18 confirmation flow)
# ══════════════════════════════════════════════════════════════════════════════

def _xp_baseline_key(event_id: int, player_id: int, skill: str) -> str:
    return f"events:{event_id}:xpbase:{player_id}:{_norm(skill)}"


def _fold_xp_baseline(redis_conn, event_id: int, player_id: int, skill, xp) -> int:
    """Return xp gained since the last stored baseline and advance it.

    The first report after join only sets the baseline (delta 0) — PRD D10:
    no retroactive credit.
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
            return 0
        prev = int(prev)
        if xp <= prev:
            return 0
        redis_conn.set(key, xp, ex=_STATE_KEY_TTL)
        return xp - prev
    except Exception:
        return 0


def _kc_dedupe(redis_conn, event_id: int, task_id: int, player_id: int, envelope: dict) -> bool:
    """True if this drop represents a not-yet-counted kill for a kc task.

    Keyed per (npc, kill_count) so multi-item drops from one kill count once;
    falls back to the submission guid when kill_count is absent.
    """
    data = envelope.get("data") or {}
    kill_count = data.get("kill_count")
    if kill_count is not None:
        member = f"{_norm(data.get('npc_name'))}:{kill_count}"
    else:
        member = f"guid:{envelope.get('guid')}"
    key = f"events:{event_id}:kcdedupe:{task_id}:{player_id}"
    try:
        added = redis_conn.sadd(key, member)
        redis_conn.expire(key, _STATE_KEY_TTL)
        return bool(added)
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


def _enqueue_notification(session, notification_type: str, event: dict,
                          player_id: int, data: dict) -> None:
    """Insert a notification_queue row (create_notification pattern —
    data/submissions/common.py). Sending is Task 19; we only enqueue."""
    if player_id is None:
        # Manual awards carry no player and notification_queue.player_id is
        # NOT NULL — those announce via the admin action itself (Task 19).
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
        _enqueue_notification(
            session,
            "event_blackout" if note == BLACKOUT_NOTE else "event_line",
            event, player_id, {
                "team_id": team_id,
                "team_name": team_name,
                "bonus_points": points,
                "line": note,
                "team_score": int(team.score or 0) if team is not None else None,
            })
        results.append({"note": note, "points": points})
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
    """Insert web_event_bingo_completions once per (cell, team), enqueue
    ``event_cell`` notifications and evaluate line/blackout bonuses. Returns
    the cell dicts that were newly completed."""
    if not cells or not event.get("has_bingo"):
        return []
    from db.models import EventBingoCompletion, EventTeam

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
        team_name = None
        if team_id is not None:
            team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
            team_name = team.name if team is not None else None
        for cell in newly:
            _enqueue_notification(session, "event_cell", event, player_id, {
                "cell_label": cell.get("label"),
                "cell_idx": cell["idx"],
                "team_id": team_id,
                "team_name": team_name,
                "task_id": task["id"],
                "task_label": task.get("label"),
                "player_name": player_name,
            })
        evaluate_bingo_bonuses(session, event, team_id,
                               trigger_task_id=task["id"],
                               player_id=player_id, player_name=player_name)
    return newly


def _current_leader(session, event_id: int):
    """(team_id, score) of the current points leader, or None."""
    from db.models import EventTeam
    row = (session.query(EventTeam)
           .filter(EventTeam.event_id == event_id)
           .order_by(EventTeam.score.desc(), EventTeam.id.asc())
           .first())
    return (row.id, int(row.score or 0)) if row else None


def apply_ledger_row(session, redis_conn, event: dict, task: dict, completion,
                     cells: Optional[list] = None,
                     player_name: Optional[str] = None) -> dict:
    """Fold one non-pending ledger row into progress / score / bingo state and
    emit SSE + notification side effects. Shared with Task 18: the confirm
    flow calls this (via :func:`apply_completion`) when a pending row is
    approved. Caller owns the transaction; this only flushes.
    """
    from db.models import EventProgress, EventTeam

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
    if _list_kind(task) in ("all_of", "assembly"):
        # Distinct-item semantics: recompute from the applied ledger instead
        # of folding quantity (which let one big stack complete the set).
        progress.progress = _distinct_item_progress(session, task, team_id, include=completion)
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
        return result

    progress.completed = True
    progress.completed_at = datetime.now()
    result["kind"] = "completion"

    team_score = None
    points = int(task.get("points") or 0)
    lead_changed_to = None
    if points and team_id is not None:
        previous_leader = _current_leader(session, event["id"])
        team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
        if team is not None:
            team.score = int(team.score or 0) + points
            team_score = team.score
            session.flush()
            new_leader = _current_leader(session, event["id"])
            if (previous_leader and new_leader
                    and new_leader[0] != previous_leader[0]
                    and new_leader[0] == team_id):
                lead_changed_to = new_leader
    result["points"] = points
    if team_score is not None:
        result["team_score"] = team_score

    new_cells = _complete_bingo_cells(
        session, event, task, team_id, player_id, cells or [],
        player_name=player_name)
    session.flush()

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

    notification = {
        "task_id": task["id"],
        "task_label": task.get("label"),
        "team_id": team_id,
        "player_id": player_id,
        "player_name": player_name,
        "points": points,
        "team_score": team_score,
        "cell_idxs": [c["idx"] for c in new_cells],
        "source_type": completion.source_type,
        "proof_url": completion.proof_url,
    }
    _enqueue_notification(session, "event_completion", event, player_id, notification)
    if lead_changed_to:
        _enqueue_notification(session, "event_lead_change", event, player_id, {
            "team_id": team_id,
            "team_score": lead_changed_to[1],
            "task_id": task["id"],
            "task_label": task.get("label"),
        })
    return result


def record_match(session, redis_conn, event: dict, task: dict, team_id: int,
                 player_id: int, quantity: int, envelope: dict,
                 cells: Optional[list] = None,
                 matched_target: Optional[str] = None) -> Optional[dict]:
    """Insert the ledger row for a match (idempotent on
    (task, team, submission_guid)); apply effects unless it needs
    confirmation. Returns a result dict, or None on duplicate replay."""
    from db.models import EventCompletion

    data = envelope.get("data") or {}
    status = "pending" if (task.get("requires_confirmation")
                           or event.get("requires_confirmation")) else "auto"
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

        xp_delta = None  # baseline folded at most once per (event, envelope)
        for task in state.tasks_by_event.get(event_id, []):
            match = match_task(task, envelope)
            if match is None:
                continue
            quantity = match["quantity"]
            if match["mode"] == "kc":
                if not _kc_dedupe(redis_conn, event_id, task["id"], player_id, envelope):
                    continue
            elif match["mode"] == "xp":
                if xp_delta is None:
                    data = envelope.get("data") or {}
                    xp_delta = _fold_xp_baseline(
                        redis_conn, event_id, player_id,
                        data.get("skill"), data.get("xp"))
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

    if _list_kind(task) in ("all_of", "assembly"):
        # Distinct-item semantics (the revoked row is already excluded — its
        # status flipped before this recompute).
        new_progress = _distinct_item_progress(session, task, team_id)
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
