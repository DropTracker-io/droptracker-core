"""Task 18 — verification queue & manual admin actions (events-prd.md A4/B4, D3/D10).

The human layer over the completion engine. All routes require event-admin
auth (group owner/admin with the events entitlement; superadmin for global
events where group_id is NULL):

  GET   /api/v1/events/{id}/completions?status=pending|all|<status>&teamId=&taskId=
        -> EventCompletion[] (joined task label / team name / player name)
  POST  /api/v1/events/{id}/completions/{completionId}/confirm            -> { ok }
  POST  /api/v1/events/{id}/completions/{completionId}/reject  { note? }  -> { ok }
  POST  /api/v1/events/{id}/award   { task_id, team_id, quantity?, note? } -> { id }
  POST  /api/v1/events/{id}/revoke  { completion_id, note? }               -> { ok }
  PATCH /api/v1/events/{id}/tasks/{taskId}  { label?, target?, target_value?,
                                              points?, requires_confirmation? }

Semantics:
- confirm/award reuse ``services.event_engine.apply_completion`` so a
  confirmed pending row (or a manual award) takes the *exact* same apply path
  as an auto completion folded by the worker.
- reject flips ``pending -> rejected`` with no side effects (note stored).
- revoke delegates to ``services.event_engine.revoke_ledger_row`` — the fold
  logic lives in the engine, never here.
- Every action writes an ``audit_log`` row with before/after JSON
  (``event.completion.confirm`` / ``.reject`` / ``event.award`` /
  ``event.revoke``).
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify, request

from db import (
    AuditLog,
    EventCompletion,
    EventTask,
    EventTeam,
    EVENT_COMPLETION_STATUSES,
    Player,
)
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import current_user_id, json_body
from web_api.routes.events import _assert_event_admin, _bump, _load_event_or_404, _ts

event_admin_bp = Blueprint("v1_event_admin", __name__)


def _engine():
    """Lazy import — the pytest conftest stubs ``services``, and web_api must
    stay importable under those stubs (same reason routes/events.py lazy-loads
    the admin bump)."""
    from services import event_engine

    return event_engine

# Ledger rows whose effects have been applied — the only revocable states.
APPLIED_STATUSES = ("auto", "confirmed", "manual")


def _completion_payload(c: EventCompletion, task_label=None, team_name=None,
                        player_name=None) -> dict:
    return {
        "id": c.id,
        "event_id": c.event_id,
        "task_id": c.task_id,
        "task_label": task_label,
        "team_id": c.team_id,
        "team_name": team_name,
        "player_id": c.player_id,
        "player_name": player_name,
        "status": c.status,
        "quantity": int(c.quantity or 1),
        "source_type": c.source_type,
        "submission_guid": c.submission_guid,
        "proof_url": c.proof_url,
        "note": c.note,
        "created_at": _ts(c.created_at),
    }


def _snapshot(c: EventCompletion) -> str:
    """Before/after JSON for audit rows."""
    return json.dumps({
        "status": c.status,
        "task_id": c.task_id,
        "team_id": c.team_id,
        "player_id": c.player_id,
        "quantity": int(c.quantity or 1),
        "source_type": c.source_type,
        "note": c.note,
        "acted_by_user_id": c.acted_by_user_id,
    })


def _load_completion_or_404(s, event_id: int, completion_id: int) -> EventCompletion:
    comp = (
        s.query(EventCompletion)
        .filter(EventCompletion.id == completion_id, EventCompletion.event_id == event_id)
        .first()
    )
    if not comp:
        abort_problem(404, "Completion not found", f"No completion {completion_id} in this event.")
    return comp


def _clean_note(body: dict) -> str | None:
    note = body.get("note")
    if note is None:
        return None
    if not isinstance(note, str):
        abort_problem(422, "Invalid note", "'note' must be a string.")
    note = note.strip()
    if len(note) > 255:
        abort_problem(422, "Invalid note", "'note' must be at most 255 characters.")
    return note or None


# --------------------------------------------------------------------------- #
# Ledger reads (verification queue + full ledger)
# --------------------------------------------------------------------------- #
@event_admin_bp.get("/events/<int:event_id>/completions")
async def list_completions(event_id: int):
    user_id = current_user_id()
    status = (request.args.get("status") or "all").strip().lower()
    if status not in ("all", *EVENT_COMPLETION_STATUSES):
        abort_problem(
            422,
            "Invalid status",
            f"status must be 'all' or one of {list(EVENT_COMPLETION_STATUSES)}.",
        )

    def _int_arg(name: str):
        raw = request.args.get(name)
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            abort_problem(422, f"Invalid {name}", f"'{name}' must be an integer.")

    team_id = _int_arg("teamId")
    task_id = _int_arg("taskId")

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)
            q = (
                s.query(EventCompletion, EventTask.label, EventTeam.name, Player.player_name)
                .outerjoin(EventTask, EventTask.id == EventCompletion.task_id)
                .outerjoin(EventTeam, EventTeam.id == EventCompletion.team_id)
                .outerjoin(Player, Player.player_id == EventCompletion.player_id)
                .filter(EventCompletion.event_id == event_id)
            )
            if status != "all":
                q = q.filter(EventCompletion.status == status)
            if team_id is not None:
                q = q.filter(EventCompletion.team_id == team_id)
            if task_id is not None:
                q = q.filter(EventCompletion.task_id == task_id)
            rows = q.order_by(EventCompletion.id.desc()).limit(500).all()
            return [
                _completion_payload(c, task_label, team_name, player_name)
                for c, task_label, team_name, player_name in rows
            ]

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Confirm / reject (verification queue)
# --------------------------------------------------------------------------- #
@event_admin_bp.post("/events/<int:event_id>/completions/<int:completion_id>/confirm")
async def confirm_completion(event_id: int, completion_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)
            comp = _load_completion_or_404(s, event_id, completion_id)
            if comp.status != "pending":
                abort_problem(
                    409,
                    "Not pending",
                    f"Completion {completion_id} is '{comp.status}'; only pending rows can be confirmed.",
                )
            before = _snapshot(comp)
            comp.status = "confirmed"
            comp.acted_by_user_id = user_id
            # Single shared apply path with the worker: a confirmed row takes
            # effect exactly like an auto completion (progress fold, points,
            # bingo cells, SSE, notification).
            _engine().apply_completion(s, comp)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                action="event.completion.confirm",
                target=f"web_event_completions.{completion_id}",
                before=before,
                after=_snapshot(comp),
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


@event_admin_bp.post("/events/<int:event_id>/completions/<int:completion_id>/reject")
async def reject_completion(event_id: int, completion_id: int):
    user_id = current_user_id()
    body = await json_body(required=False)
    note = _clean_note(body or {})

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)
            comp = _load_completion_or_404(s, event_id, completion_id)
            if comp.status != "pending":
                abort_problem(
                    409,
                    "Not pending",
                    f"Completion {completion_id} is '{comp.status}'; only pending rows can be rejected.",
                )
            before = _snapshot(comp)
            # pending -> rejected: no side effects, note stored.
            comp.status = "rejected"
            comp.acted_by_user_id = user_id
            if note:
                comp.note = note
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                action="event.completion.reject",
                target=f"web_event_completions.{completion_id}",
                before=before,
                after=_snapshot(comp),
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


# --------------------------------------------------------------------------- #
# Manual award / revoke (always available to event admins — PRD D3/D10)
# --------------------------------------------------------------------------- #
@event_admin_bp.post("/events/<int:event_id>/award")
async def award_completion(event_id: int):
    """Insert a ``manual`` ledger row and apply it immediately — the escape
    hatch for pre-join credit (D10) and custom/ehp/ehb tasks."""
    user_id = current_user_id()
    body = await json_body()
    task_id = body.get("task_id")
    team_id = body.get("team_id")
    if not isinstance(task_id, int):
        abort_problem(422, "Invalid task_id", "'task_id' must be an integer.")
    if not isinstance(team_id, int):
        abort_problem(422, "Invalid team_id", "'team_id' must be an integer.")
    quantity = body.get("quantity", 1)
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        abort_problem(422, "Invalid quantity", "'quantity' must be a positive integer.")
    note = _clean_note(body)

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)
            task = (
                s.query(EventTask)
                .filter(EventTask.id == task_id, EventTask.event_id == event_id)
                .first()
            )
            if not task:
                abort_problem(404, "Task not found", f"No task {task_id} in this event.")
            team = (
                s.query(EventTeam)
                .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
                .first()
            )
            if not team:
                abort_problem(404, "Team not found", f"No team {team_id} in this event.")
            comp = EventCompletion(
                event_id=event_id,
                task_id=task_id,
                team_id=team_id,
                player_id=None,
                status="manual",
                quantity=quantity,
                source_type="manual",
                acted_by_user_id=user_id,
                note=note,
            )
            s.add(comp)
            s.flush()
            # Same shared apply path as auto/confirmed rows.
            _engine().apply_completion(s, comp)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                action="event.award",
                target=f"web_event_completions.{comp.id}",
                before=None,
                after=_snapshot(comp),
            ))
            s.commit()
            return comp.id

    comp_id = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"id": comp_id}))


@event_admin_bp.post("/events/<int:event_id>/revoke")
async def revoke_completion(event_id: int):
    user_id = current_user_id()
    body = await json_body()
    completion_id = body.get("completion_id")
    if not isinstance(completion_id, int):
        abort_problem(422, "Invalid completion_id", "'completion_id' must be an integer.")
    note = _clean_note(body)

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)
            comp = _load_completion_or_404(s, event_id, completion_id)
            if comp.status not in APPLIED_STATUSES:
                abort_problem(
                    409,
                    "Not revocable",
                    f"Completion {completion_id} is '{comp.status}'; only applied "
                    f"({'/'.join(APPLIED_STATUSES)}) rows can be revoked.",
                )
            before = _snapshot(comp)
            comp.status = "revoked"
            comp.acted_by_user_id = user_id
            if note:
                comp.note = note
            # Engine-owned recompute: re-folds the (task, team) rollup from
            # surviving rows, adjusts the score, unwinds bingo cells and
            # publishes the SSE correction.
            summary = _engine().revoke_ledger_row(s, comp)
            after = json.loads(_snapshot(comp))
            after["recomputed"] = summary
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                action="event.revoke",
                target=f"web_event_completions.{completion_id}",
                before=before,
                after=json.dumps(after),
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


# --------------------------------------------------------------------------- #
# Per-task edits (requires_confirmation toggle etc. — PRD D3)
# --------------------------------------------------------------------------- #
@event_admin_bp.patch("/events/<int:event_id>/tasks/<int:task_id>")
async def update_task(event_id: int, task_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)
            task = (
                s.query(EventTask)
                .filter(EventTask.id == task_id, EventTask.event_id == event_id)
                .first()
            )
            if not task:
                abort_problem(404, "Task not found", f"No task {task_id} in this event.")
            if "label" in body:
                label = (body.get("label") or "").strip()
                if not (1 <= len(label) <= 255):
                    abort_problem(422, "Invalid label", "Task label must be 1–255 characters.")
                task.label = label
            if "target" in body:
                target = body.get("target")
                if target is not None and not isinstance(target, str):
                    abort_problem(422, "Invalid target", "'target' must be a string or null.")
                task.target = (target or "").strip()[:120] or None
            if "target_value" in body:
                tv = body.get("target_value")
                if tv is not None and (not isinstance(tv, int) or isinstance(tv, bool) or tv < 0):
                    abort_problem(
                        422,
                        "Invalid target_value",
                        "'target_value' must be a non-negative integer or null.",
                    )
                task.target_value = tv
            if "points" in body:
                points = body.get("points")
                if not isinstance(points, int) or isinstance(points, bool) or points < 0:
                    abort_problem(422, "Invalid points", "'points' must be a non-negative integer.")
                task.points = points
            if "requires_confirmation" in body:
                task.requires_confirmation = bool(body.get("requires_confirmation"))
            s.commit()
            return {
                "id": task.id,
                "type": task.type,
                "label": task.label,
                "target": task.target or None,
                "target_value": task.target_value,
                "points": int(task.points or 0),
                "requires_confirmation": bool(task.requires_confirmation),
            }

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))
