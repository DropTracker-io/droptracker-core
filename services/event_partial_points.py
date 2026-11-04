"""Partial points for unfinished "points from a list" tasks (web156a).

A ``point_collection`` task ("Points from a list") pays its task points only
once the team reaches the points goal. With ``Event.partial_points`` on, a
team that ends the event short of the goal is paid the same share of the
task's points as the share of the goal it reached, rounded down: 6 of a
10-point goal on a 10-point task pays 6. A repeatable task pays the unfinished
lap the same way. Nothing changes while the event runs; the payout lands when
it ends, before the final standings are read.

The payout is one ledger row per (task, team):
``source_type='bonus'`` (so no progress fold counts it, like a line bonus)
with ``submission_guid = PARTIAL_GUID``. The guid is the marker, not the
note: the admin revoke route overwrites a row's note, never its guid, and the
``uq_web_evt_completion_src`` unique key keeps it to one row per task/team.
The row's ``quantity`` is the points it paid, so a re-sync moves the team
score by exactly the difference.

:func:`sync_partial_points` is idempotent and is re-run whenever something
could change the answer after the end: a late approval or revoke, a task
edit, or the setting being flipped on a past event. A partial row an admin
revoked by hand is left alone (and never re-paid).

Imports only stdlib + utils at module level (models are lazy), so the unit
tests load it straight from the file.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from utils import task_repeat as _tr

PARTIAL_GUID = "partial-points"
PARTIAL_NOTE = "Partial points: unfinished when the event ended"
# Bingo boards are standard events with has_bingo set. Board-game, Conquest,
# Loot Sweep and SOTW/BOTW rank on their own rules, not task points.
PARTIAL_EVENT_KINDS = ("standard",)
_APPLIED = ("auto", "confirmed", "manual")


def _config(raw) -> dict:
    return _tr._config(raw)


def is_partial_task(task_type, config) -> bool:
    """Whether a task can earn partial points: a "points from a list" task."""
    return task_type == "item_collection" and _config(config).get("kind") == "point_collection"


def partial_enabled(event) -> bool:
    """The setting is on and the event is a kind it applies to."""
    return (bool(getattr(event, "partial_points", False))
            and (getattr(event, "kind", None) or "standard") in PARTIAL_EVENT_KINDS)


def partial_due(points, threshold, progress, completed, cap=1) -> int:
    """Points owed for the unfinished part of one (task, team) rollup.

    ``points`` per completion, ``threshold`` the points goal, ``progress`` the
    rollup total, ``cap`` from :func:`utils.task_repeat.repeat_cap`. A finished
    task (or a repeatable one at its cap) owes nothing more."""
    try:
        points = int(points or 0)
        threshold = max(int(threshold or 1), 1)
        progress = max(float(progress or 0), 0.0)
    except (TypeError, ValueError):
        return 0
    if points <= 0:
        return 0
    count = _tr.completion_count(progress, threshold, completed, cap)
    if _tr.is_maxed(count, cap):
        return 0
    leftover = progress if cap == 1 else progress - count * threshold
    leftover = min(max(leftover, 0.0), float(threshold))
    # Epsilon: 0.7 * 10 / 7 must floor to 1, not 0.9999.
    return max(int(math.floor(points * leftover / threshold + 1e-9)), 0)


def _set_score(team, delta: int) -> None:
    value = round(float(team.score or 0) + delta, 2)
    team.score = int(value) if value == int(value) else value


def sync_partial_points(session, event, *, task_ids: Optional[Iterable[int]] = None,
                        team_ids: Optional[Iterable[int]] = None) -> dict:
    """Bring each (task, team) partial row in line with what is owed now.

    Owed is :func:`partial_due` when the event is over and the setting is on,
    else 0 (so turning the setting off, or a late completion, takes the
    partial payout back). ``task_ids``/``team_ids`` narrow the pass. Caller
    owns the commit; this only flushes. Returns ``{team_id: delta}`` for the
    teams whose score moved."""
    from db.models import EventCompletion, EventProgress, EventTask, EventTeam

    q = session.query(EventTask).filter(EventTask.event_id == event.id)
    if task_ids is not None:
        task_ids = [t for t in task_ids if t is not None]
        if not task_ids:
            return {}
        q = q.filter(EventTask.id.in_(task_ids))
    tasks = [t for t in q.all() if is_partial_task(t.type, t.config)]
    if not tasks:
        return {}
    tq = session.query(EventTeam.id).filter(EventTeam.event_id == event.id)
    if team_ids is not None:
        team_ids = [t for t in team_ids if t is not None]
        if not team_ids:
            return {}
        tq = tq.filter(EventTeam.id.in_(team_ids))
    teams = [tid for (tid,) in tq.all()]
    if not teams:
        return {}

    task_id_list = [t.id for t in tasks]
    progress = {
        (p.task_id, p.team_id): p
        for p in session.query(EventProgress).filter(
            EventProgress.task_id.in_(task_id_list),
            EventProgress.team_id.in_(teams)).all()
    }
    existing = {
        (c.task_id, c.team_id): c
        for c in session.query(EventCompletion).filter(
            EventCompletion.event_id == event.id,
            EventCompletion.task_id.in_(task_id_list),
            EventCompletion.team_id.in_(teams),
            EventCompletion.source_type == "bonus",
            EventCompletion.submission_guid == PARTIAL_GUID).all()
    }

    enabled = getattr(event, "status", None) == "past" and partial_enabled(event)
    kind = getattr(event, "kind", None) or "standard"
    deltas: dict = {}
    for task in tasks:
        cap = _tr.repeat_cap(task.type, task.config, event_kind=kind)
        try:
            threshold = max(int(task.target_value or 0), 1)
        except (TypeError, ValueError):
            threshold = 1
        for team_id in teams:
            row = existing.get((task.id, team_id))
            if row is not None and row.status not in _APPLIED:
                continue  # revoked by an admin: their call stands
            prog = progress.get((task.id, team_id))
            due = 0
            if enabled and prog is not None:
                due = partial_due(task.points, threshold, prog.progress,
                                  bool(prog.completed), cap)
            paid = max(int(row.quantity or 0), 0) if row is not None else 0
            if due == paid:
                continue
            if row is None:
                session.add(EventCompletion(
                    event_id=event.id, task_id=task.id, team_id=team_id,
                    player_id=None, status="auto", quantity=due,
                    source_type="bonus", submission_guid=PARTIAL_GUID,
                    note=PARTIAL_NOTE))
            elif due == 0:
                session.delete(row)
            else:
                row.quantity = due
            deltas[team_id] = deltas.get(team_id, 0) + (due - paid)

    for team_id, delta in deltas.items():
        if not delta:
            continue
        team = (session.query(EventTeam).filter(EventTeam.id == team_id)
                .with_for_update().first())  # P0-7: locked score RMW
        if team is not None:
            _set_score(team, delta)
    session.flush()
    moved = {tid: d for tid, d in deltas.items() if d}
    if moved:
        try:
            from services.event_engine import _publish

            for tid in moved:
                team = session.query(EventTeam).filter(EventTeam.id == tid).first()
                _publish(event.id, {
                    "kind": "recompute", "event_id": event.id, "team_id": tid,
                    "team_score": team.score if team is not None else None,
                    "reason": "partial_points",
                })
        except Exception:
            pass  # SSE is best-effort; the score is already written
    return moved


def resync_after_correction(session, event, task_id=None, team_id=None,
                            completion=None) -> dict:
    """Engine hook after a late approval / revoke / task edit: re-sync only
    on a past event, and never for the partial row itself (revoking it is the
    admin taking the payout back)."""
    if getattr(event, "status", None) != "past":
        return {}
    if completion is not None and getattr(completion, "submission_guid", None) == PARTIAL_GUID:
        return {}
    return sync_partial_points(
        session, event,
        task_ids=[task_id] if task_id is not None else None,
        team_ids=[team_id] if team_id is not None else None)
