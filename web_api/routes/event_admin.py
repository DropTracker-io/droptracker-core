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

Task 20 (bingo designer):
  PUT   /api/v1/events/{id}/bingo   { size, cells: [{ idx, label?, task_id? |
                                      library_item_id? | new_task?, points? }] }
        -> EventDetail.bingo   (replaces the whole board; 409 once started)
  GET   /api/v1/event-task-library?query=&type=&page=  -> EventTaskLibraryItem[]
        (session + any group admin / superadmin)

Task-library management (superadmin CP — curated/global presets shape every
clan's pickers):
  POST   /api/v1/event-task-library        { name, type, goal…, default_points?,
                                             difficulty?, visibility? } -> row
  PATCH  /api/v1/event-task-library/{id}   { partial }                  -> row
  DELETE /api/v1/event-task-library/{id}   -> { ok }   (soft: active=false)

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
from datetime import datetime

from quart import Blueprint, jsonify, request
from sqlalchemy import func

from db import (
    AuditLog,
    Event,
    EventBingoCell,
    EventBingoCompletion,
    EventCompletion,
    EventProgress,
    EventTask,
    EventTaskLibraryItem,
    EventTeam,
    EVENT_BOARD_SIZES,
    EVENT_COMPLETION_STATUSES,
    EVENT_TASK_TYPES,
    GroupAdmin,
    Player,
)
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
)
from web_api.routes.events import (
    _assert_event_admin,
    _bump,
    _detail,
    _effective_status,
    _is_event_admin,
    _load_event_or_404,
    _ts,
    clean_task_visibility,
    save_task_to_library,
)

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


def _load_completion_or_404(
    s, event_id: int, completion_id: int, *, for_update: bool = False
) -> EventCompletion:
    q = s.query(EventCompletion).filter(
        EventCompletion.id == completion_id, EventCompletion.event_id == event_id
    )
    # P0-4: confirm/reject lock the row so two admins acting concurrently (or a
    # double-click) serialize — the second re-reads a non-pending status under
    # the lock and 409s instead of double-applying points/coins/auto-roll.
    if for_update:
        q = q.with_for_update()
    comp = q.first()
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
# Cross-event pending queue (Discord Activity review prompt)
# --------------------------------------------------------------------------- #
@event_admin_bp.get("/events/pending-review")
async def my_pending_reviews():
    """Pending completions awaiting the session user's confirmation, grouped
    by event, across every ACTIVE event they administer. Powers the Discord
    Activity's "awaiting review" pop-up and badges. Events the user can't
    admin are silently absent — this is a personal work queue, not an audit
    surface. Each event carries its newest pending rows (capped) plus the
    true total, so the client can show a preview without paging."""
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            counts = (
                s.query(EventCompletion.event_id, func.count(EventCompletion.id))
                .join(Event, Event.id == EventCompletion.event_id)
                .filter(EventCompletion.status == "pending", Event.status == "active")
                .group_by(EventCompletion.event_id)
                .all()
            )
            if not counts:
                return []
            events = {
                ev.id: ev
                for ev in s.query(Event)
                .filter(Event.id.in_([eid for eid, _ in counts]))
                .all()
            }
            out = []
            for event_id, count in counts:
                ev = events.get(event_id)
                if ev is None or not _is_event_admin(s, user_id, ev):
                    continue
                rows = (
                    s.query(EventCompletion, EventTask.label, EventTeam.name,
                            Player.player_name)
                    .outerjoin(EventTask, EventTask.id == EventCompletion.task_id)
                    .outerjoin(EventTeam, EventTeam.id == EventCompletion.team_id)
                    .outerjoin(Player, Player.player_id == EventCompletion.player_id)
                    .filter(EventCompletion.event_id == event_id,
                            EventCompletion.status == "pending")
                    .order_by(EventCompletion.id.desc())
                    .limit(10)
                    .all()
                )
                out.append({
                    "event_id": event_id,
                    "event_name": ev.name,
                    "group_id": ev.group_id,
                    "pending_count": int(count),
                    "completions": [
                        _completion_payload(c, task_label, team_name, player_name)
                        for c, task_label, team_name, player_name in rows
                    ],
                })
            out.sort(key=lambda e: -e["pending_count"])
            return out

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


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
            _assert_event_admin(s, user_id, ev)
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
def _publish_pending_update(s, ev, comp) -> None:
    """After a confirm/reject, push a fresh ``kind: "pending"`` SSE frame so
    the board's amber tint updates (or clears) live (web53a). Best-effort —
    a realtime hiccup must never fail the admin action."""
    try:
        from services.event_engine import _publish, pending_projection

        task = s.query(EventTask).filter(EventTask.id == comp.task_id).first()
        if task is None or comp.team_id is None:
            return
        config = task.config
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (ValueError, TypeError):
                config = None
        proj = pending_projection(
            s, {"id": task.id, "target_value": task.target_value,
                "config": config}, comp.team_id)
        frame = {
            "kind": "pending", "event_id": ev.id, "task_id": comp.task_id,
            "team_id": comp.team_id,
            "pending": proj["pending_count"] if proj else 0,
            "pending_complete": bool(proj and proj["pending_complete"]),
        }
        if proj:
            frame["progress"] = proj["applied"]
        _publish(ev.id, frame)
    except Exception:
        pass


@event_admin_bp.post("/events/<int:event_id>/completions/<int:completion_id>/confirm")
async def confirm_completion(event_id: int, completion_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            comp = _load_completion_or_404(s, event_id, completion_id, for_update=True)
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
                event_id=ev.id,
                action="event.completion.confirm",
                target=f"web_event_completions.{completion_id}",
                before=before,
                after=_snapshot(comp),
            ))
            s.commit()
            _publish_pending_update(s, ev, comp)

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
            _assert_event_admin(s, user_id, ev)
            comp = _load_completion_or_404(s, event_id, completion_id, for_update=True)
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
                event_id=ev.id,
                action="event.completion.reject",
                target=f"web_event_completions.{completion_id}",
                before=before,
                after=_snapshot(comp),
            ))
            s.commit()
            _publish_pending_update(s, ev, comp)

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
    complete = bool(body.get("complete"))
    note = _clean_note(body)

    def _apply():
        nonlocal quantity
        # P0-10: manual rows have no submission_guid, so the ledger's unique
        # (task, team, guid) index can't dedupe them — an organizer's double
        # click would insert and apply two identical awards. A short one-shot
        # Redis claim absorbs the double click before any DB work.
        fresh_click = True
        try:
            from utils.redis import redis_client

            claim = f"events:{event_id}:awardclick:{task_id}:{team_id}:{user_id}"
            conn = getattr(redis_client, "client", None) or redis_client
            fresh_click = bool(conn.set(claim, 1, nx=True, ex=5))
        except Exception:
            pass  # Redis down → fall through; the progress lock still guards
        if not fresh_click:
            abort_problem(
                409, "Award already in flight",
                "An identical award was applied moments ago — refresh to "
                "see it. (If you really meant to award twice, wait a few "
                "seconds and try again.)")
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
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
            if complete:
                # "Mark complete": fill whatever progress is left so this one
                # ledger row crosses the task's threshold (an award of the
                # default quantity 1 on a 50-KC task otherwise just records
                # 1/50 and completes nothing). Locked read (P0-10): a
                # concurrent award/confirm must not both see the same "left".
                eng = _engine()
                threshold = eng.completion_threshold(eng._task_to_dict(task))
                current = (
                    s.query(EventProgress)
                    .filter(EventProgress.task_id == task_id,
                            EventProgress.team_id == team_id)
                    .with_for_update()
                    .first()
                )
                if current is not None and current.completed:
                    abort_problem(
                        409, "Already complete",
                        "This task is already complete for that team — "
                        "nothing to mark.")
                done = int(current.progress or 0) if current else 0
                quantity = max(threshold - done, 1)
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
                event_id=ev.id,
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
            _assert_event_admin(s, user_id, ev)
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
                event_id=ev.id,
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
            _assert_event_admin(s, user_id, ev)
            task = (
                s.query(EventTask)
                .filter(EventTask.id == task_id, EventTask.event_id == event_id)
                .first()
            )
            if not task:
                abort_problem(404, "Task not found", f"No task {task_id} in this event.")
            _before_task = {
                "label": task.label, "points": int(task.points or 0),
                "target": task.target, "target_value": task.target_value,
                "visibility": task.visibility,
            }
            if "label" in body:
                label = (body.get("label") or "").strip()
                if not (1 <= len(label) <= 255):
                    abort_problem(422, "Invalid label", "Task label must be 1–255 characters.")
                task.label = label
            if "target" in body or "target_value" in body or "config" in body:
                # Re-validate the merged goal per task type so an edit can't
                # leave a task the engine will never match. `config` edits let
                # the task form change item lists / source NPCs in place
                # (instead of the old delete-and-recreate flow); explicit
                # `config: null` switches an item_collection back to
                # single-item semantics.
                from web_api.routes.event_task_validation import validate_task_payload

                target = body.get("target", task.target)
                if target is not None and not isinstance(target, str):
                    abort_problem(422, "Invalid target", "'target' must be a string or null.")
                config = body.get("config", task.config)
                if config is not None and not isinstance(config, (str, dict)):
                    abort_problem(422, "Invalid config", "'config' must be a JSON object, string or null.")
                normalized = validate_task_payload(s, {
                    "type": task.type,
                    "target": target,
                    "target_value": body.get("target_value", task.target_value),
                    "config": config,
                })
                if "config" in body and task.config:
                    # A replacement config from the form must not strip the
                    # designer's auto-created marker off a board task.
                    try:
                        old_cfg = json.loads(task.config)
                        if isinstance(old_cfg, dict) and old_cfg.get(_BINGO_AUTO_KEY):
                            new_cfg = json.loads(normalized["config"]) if normalized["config"] else {}
                            if isinstance(new_cfg, dict) and _BINGO_AUTO_KEY not in new_cfg:
                                new_cfg[_BINGO_AUTO_KEY] = old_cfg[_BINGO_AUTO_KEY]
                                normalized["config"] = json.dumps(new_cfg)
                    except (TypeError, ValueError):
                        pass
                task.target = normalized["target"]
                task.target_value = normalized["target_value"]
                task.config = normalized["config"]
            if "points" in body:
                points = body.get("points")
                if not isinstance(points, int) or isinstance(points, bool) or points < 0:
                    abort_problem(422, "Invalid points", "'points' must be a non-negative integer.")
                task.points = points
            if "requires_confirmation" in body:
                task.requires_confirmation = bool(body.get("requires_confirmation"))
            if "difficulty" in body:
                # Board-game tier (web44a). Null clears it. Per the product
                # brief, editing here also updates the task's library copy
                # ("change it if it already exists") via the save below when
                # visibility is passed, or directly when it isn't.
                diff = body.get("difficulty")
                if diff is not None and diff not in _LIBRARY_DIFFICULTIES:
                    abort_problem(
                        422, "Invalid difficulty",
                        f"difficulty must be one of {list(_LIBRARY_DIFFICULTIES)} or null.",
                    )
                task.difficulty = diff
            # Absent key ⇒ leave the task's library copy untouched (the
            # quick-toggles PATCH single fields); when present, re-save the
            # preset with the chosen publicity.
            visibility = clean_task_visibility(body, default=None)
            if visibility is not None:
                task.visibility = save_task_to_library(s, ev, task, visibility)
            _after_task = {
                "label": task.label, "points": int(task.points or 0),
                "target": task.target, "target_value": task.target_value,
                "visibility": task.visibility,
            }
            if _after_task != _before_task:
                # A mid-event points/goal edit silently rescores — record it so
                # the audit log can explain a task's point total (web57a).
                s.add(AuditLog(
                    actor_user_id=user_id, group_id=ev.group_id, event_id=event_id,
                    action="event.task.update",
                    target=f"web_event_tasks.{task.id}",
                    before=json.dumps(_before_task),
                    after=json.dumps(_after_task),
                ))
            s.commit()
            return {
                "id": task.id,
                "type": task.type,
                "label": task.label,
                "target": task.target or None,
                "target_value": task.target_value,
                "points": int(task.points or 0),
                "requires_confirmation": bool(task.requires_confirmation),
                "visibility": task.visibility or "public",
                "config": task.config or None,
            }

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Bingo board designer (Task 20)
# --------------------------------------------------------------------------- #
# Marker merged into auto-created tasks' config JSON so a later board replace
# can tell designer-created tasks (safe to garbage-collect once orphaned)
# from hand-added ones.
_BINGO_AUTO_KEY = "bingo_auto"


def _assert_board_editable(ev) -> None:
    """409 unless the board is still editable.

    Task 21 (explicit lifecycle) hasn't landed — today create_event sets
    status='active' immediately, so "draft only" would lock every board.
    Until then the gate is "not started": draft, or never explicitly
    activated and the scheduled start (if any) is still in the future.
    Task 21's explicit activation naturally tightens this to draft-only.
    """
    if ev.status == "draft":
        return
    if ev.activated_at is None and (ev.starts_at is None or ev.starts_at > datetime.now()):
        return
    abort_problem(
        409, "Event has started",
        "The bingo board is locked once the event starts.",
    )


def _merged_auto_config(raw) -> str:
    """Task config JSON with the designer's auto-created marker merged in."""
    config = {}
    if isinstance(raw, dict):
        config = dict(raw)
    elif raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                config = parsed
        except (TypeError, ValueError):
            config = {}
    config[_BINGO_AUTO_KEY] = True
    return json.dumps(config)


def _is_auto_created(task: EventTask) -> bool:
    try:
        return bool(json.loads(task.config or "{}").get(_BINGO_AUTO_KEY))
    except (TypeError, ValueError):
        return False


def _task_identity(type_, label, target, target_value, points,
                   requires_confirmation, config) -> tuple:
    """Content identity of a task row for board-save reuse.

    A library pick / inline new task whose every field matches an existing
    task row (the ``bingo_auto`` marker aside) binds that row instead of
    cloning it. Config comparison is key-order/whitespace-insensitive and
    label/target follow the DB's case-insensitive collation."""
    canonical_config = None
    if config:
        if isinstance(config, dict):
            parsed = dict(config)
        else:
            try:
                parsed = json.loads(config)
            except (TypeError, ValueError):
                parsed = None
        if isinstance(parsed, dict):
            parsed.pop(_BINGO_AUTO_KEY, None)
            canonical_config = json.dumps(parsed, sort_keys=True) if parsed else None
        else:
            canonical_config = str(config)
    return (
        type_,
        (label or "").strip()[:255].lower(),
        ((target or "").strip().lower() or None),
        target_value,
        int(points or 0),
        bool(requires_confirmation),
        canonical_config,
    )


def _clean_cell_points(cell: dict):
    points = cell.get("points")
    if points is None:
        return None
    if not isinstance(points, int) or isinstance(points, bool) or points < 0:
        abort_problem(422, "Invalid points", "cell 'points' must be a non-negative integer.")
    return points


def _validate_board_body(body: dict) -> tuple[int, list[dict]]:
    size = body.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size not in EVENT_BOARD_SIZES:
        abort_problem(
            422, "Invalid size",
            f"size must be one of {list(EVENT_BOARD_SIZES)} (square boards only).",
        )
    cells = body.get("cells")
    if not isinstance(cells, list) or len(cells) != size * size:
        abort_problem(
            422, "Invalid cells",
            f"cells must be a list of exactly {size * size} entries for a "
            f"{size}×{size} board.",
        )
    seen: set[int] = set()
    for cell in cells:
        if not isinstance(cell, dict):
            abort_problem(422, "Invalid cell", "Each cell must be an object.")
        idx = cell.get("idx")
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < size * size):
            abort_problem(
                422, "Invalid cell idx",
                f"cell idx must be an integer in [0, {size * size - 1}].",
            )
        if idx in seen:
            abort_problem(422, "Duplicate cell idx", f"cell idx {idx} appears twice.")
        seen.add(idx)
        bindings = [k for k in ("task_id", "library_item_id", "new_task") if cell.get(k) is not None]
        if len(bindings) > 1:
            abort_problem(
                422, "Ambiguous cell",
                "Each cell takes exactly one of task_id, library_item_id or "
                "new_task — or none of them for a free cell.",
            )
        label = cell.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 255):
            abort_problem(422, "Invalid label", "cell label must be a string of at most 255 characters.")
        if "task_id" in bindings and not isinstance(cell["task_id"], int):
            abort_problem(422, "Invalid task_id", "cell 'task_id' must be an integer.")
        if "library_item_id" in bindings and not isinstance(cell["library_item_id"], int):
            abort_problem(422, "Invalid library_item_id", "cell 'library_item_id' must be an integer.")
        if "new_task" in bindings:
            nt = cell["new_task"]
            if not isinstance(nt, dict):
                abort_problem(422, "Invalid new_task", "cell 'new_task' must be an object.")
            if nt.get("type") not in EVENT_TASK_TYPES:
                abort_problem(
                    422, "Invalid task type",
                    f"new_task.type must be one of {list(EVENT_TASK_TYPES)}.",
                )
            if not (nt.get("label") or "").strip():
                abort_problem(422, "Invalid new_task", "new_task.label is required.")
        _clean_cell_points(cell)
    return size, cells


@event_admin_bp.put("/events/<int:event_id>/bingo")
async def put_bingo_board(event_id: int):
    """Replace the event's whole bingo board (designer save, Task 20).

    Creates tasks for library picks / inline new tasks — binding an existing
    identical task row instead of inserting a clone next to it — deletes
    auto-created tasks the new board no longer references, sets
    has_bingo/board_size, and — when the event is already live (implicit
    lifecycle) — grants free cells to every team immediately.
    """
    user_id = current_user_id()
    body = await json_body()
    size, cells_in = _validate_board_body(body)

    def _apply():
        from web_api.routes.event_task_validation import validate_task_payload

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_board_editable(ev)

            # Resolve library presets up front.
            lib_ids = {c["library_item_id"] for c in cells_in if c.get("library_item_id") is not None}
            presets = {}
            if lib_ids:
                presets = {
                    p.id: p
                    for p in s.query(EventTaskLibraryItem).filter(
                        EventTaskLibraryItem.id.in_(lib_ids),
                        EventTaskLibraryItem.active.is_(True),
                    ).all()
                }
                missing = lib_ids - set(presets)
                if missing:
                    abort_problem(
                        404, "Library item not found",
                        f"No active library item(s) {sorted(missing)}.",
                    )

            # Existing tasks referenced directly must belong to this event.
            ref_ids = {c["task_id"] for c in cells_in if c.get("task_id") is not None}
            event_tasks = s.query(EventTask).filter(
                EventTask.event_id == event_id).all()
            existing_tasks = {t.id: t for t in event_tasks if t.id in ref_ids}
            missing = ref_ids - set(existing_tasks)
            if missing:
                abort_problem(
                    404, "Task not found",
                    f"No task(s) {sorted(missing)} in this event.",
                )

            # Library picks / inline new tasks bind an existing identical task
            # row instead of inserting a clone — cloning left the original
            # orphaned in the task list forever (the engine only matches
            # cell-bound tasks on bingo events). Tasks referenced by task_id
            # cells stay reserved for those cells, and each row binds at most
            # one cell per save.
            reuse_pool: dict[tuple, list] = {}
            for t in event_tasks:
                if t.id in ref_ids:
                    continue
                reuse_pool.setdefault(_task_identity(
                    t.type, t.label, t.target, t.target_value,
                    t.points, t.requires_confirmation, t.config,
                ), []).append(t)
            for candidates in reuse_pool.values():
                # Hand-added originals before designer clones, oldest first,
                # so re-saving an already-polluted board binds the original
                # and lets the garbage collector below drop the clone.
                candidates.sort(key=lambda t: (_is_auto_created(t), t.id))

            def _claim_identical(identity: tuple):
                candidates = reuse_pool.get(identity)
                return candidates.pop(0) if candidates else None

            # Tear down the old board (pre-start boards have no earned
            # completions; free-cell grants are simply re-granted below).
            old_cells = s.query(EventBingoCell).filter(
                EventBingoCell.event_id == event_id).all()
            old_auto_task_ids = set()
            if old_cells:
                old_cell_ids = [c.id for c in old_cells]
                old_task_ids = {c.task_id for c in old_cells if c.task_id is not None}
                if old_task_ids:
                    for t in s.query(EventTask).filter(EventTask.id.in_(old_task_ids)).all():
                        if _is_auto_created(t):
                            old_auto_task_ids.add(t.id)
                (s.query(EventBingoCompletion)
                 .filter(EventBingoCompletion.cell_id.in_(old_cell_ids))
                 .delete(synchronize_session=False))
                (s.query(EventBingoCell)
                 .filter(EventBingoCell.event_id == event_id)
                 .delete(synchronize_session=False))

            # Build the new board.
            new_task_ids = set()
            for cell in sorted(cells_in, key=lambda c: c["idx"]):
                task_id = None
                label = (cell.get("label") or "").strip()
                if cell.get("task_id") is not None:
                    task = existing_tasks[cell["task_id"]]
                    task_id = task.id
                    label = label or task.label
                elif cell.get("library_item_id") is not None:
                    preset = presets[cell["library_item_id"]]
                    points = _clean_cell_points(cell)
                    if points is None:
                        points = int(preset.default_points or 0)
                    task = _claim_identical(_task_identity(
                        preset.type, preset.name, preset.target,
                        preset.target_value, points, False, preset.config,
                    ))
                    if task is None:
                        task = EventTask(
                            event_id=event_id,
                            type=preset.type,
                            label=preset.name,
                            target=preset.target,
                            target_value=preset.target_value,
                            points=points,
                            requires_confirmation=False,
                            config=_merged_auto_config(preset.config),
                        )
                        s.add(task)
                        s.flush()
                    task_id = task.id
                    label = label or preset.name
                elif cell.get("new_task") is not None:
                    nt = cell["new_task"]
                    points = nt.get("points", 0)
                    if not isinstance(points, int) or isinstance(points, bool) or points < 0:
                        abort_problem(422, "Invalid points", "new_task.points must be a non-negative integer.")
                    visibility = clean_task_visibility(nt)
                    normalized = validate_task_payload(s, nt)
                    task = _claim_identical(_task_identity(
                        nt["type"], nt["label"], normalized["target"],
                        normalized["target_value"], points,
                        bool(nt.get("requires_confirmation")), normalized["config"],
                    ))
                    if task is None:
                        task = EventTask(
                            event_id=event_id,
                            type=nt["type"],
                            label=nt["label"].strip()[:255],
                            target=normalized["target"],
                            target_value=normalized["target_value"],
                            points=points,
                            requires_confirmation=bool(nt.get("requires_confirmation")),
                            visibility=visibility,
                            config=_merged_auto_config(normalized["config"]),
                        )
                        s.add(task)
                        task.visibility = save_task_to_library(s, ev, task, visibility)
                        s.flush()
                    task_id = task.id
                    label = label or task.label
                else:
                    label = label or "Free space"
                if task_id is not None:
                    new_task_ids.add(task_id)
                s.add(EventBingoCell(
                    event_id=event_id, idx=cell["idx"],
                    label=label[:255], task_id=task_id,
                ))

            # Garbage-collect designer-created tasks the board dropped —
            # but never ones that already accrued ledger rows.
            orphaned = old_auto_task_ids - new_task_ids
            for task_id in sorted(orphaned):
                has_ledger = (s.query(EventCompletion.id)
                              .filter(EventCompletion.task_id == task_id)
                              .first())
                if has_ledger:
                    continue
                (s.query(EventProgress)
                 .filter(EventProgress.task_id == task_id)
                 .delete(synchronize_session=False))
                (s.query(EventTask)
                 .filter(EventTask.id == task_id, EventTask.event_id == event_id)
                 .delete(synchronize_session=False))

            ev.has_bingo = True
            ev.board_size = size
            # Keep the game-format dimension consistent (web43a): saving a
            # bingo board makes this a bingo event unless it's already a
            # richer kind (board_game keeps its kind).
            if (getattr(ev, "kind", None) or "standard") == "standard":
                ev.kind = "bingo"
            s.flush()

            # Free cells complete for every team "at activation". With the
            # implicit lifecycle an event without a future start is already
            # live the moment its board is saved — grant right away. Task 21
            # moves this to the explicit activation action.
            live_now = (_effective_status(ev) == "active"
                        and (ev.starts_at is None or ev.starts_at <= datetime.now()))
            if live_now:
                _engine().grant_free_cells(s, ev)
                # A live replace can invalidate previously-awarded bonuses
                # (or instantly satisfy new all-free lines) — re-derive them.
                _engine().reconcile_bingo_bonuses(s, ev)

            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                event_id=ev.id,
                action="event.bingo.replace",
                target=f"web_events.{event_id}.bingo",
                before=None,
                after=json.dumps({"size": size, "tasks": sorted(new_task_ids)}),
            ))
            s.commit()
            return _detail(s, ev, viewer_id=user_id)["bingo"]

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Task library (Task 20 designer picker)
# --------------------------------------------------------------------------- #
_LIBRARY_PAGE_SIZE = 50


def _admin_group_ids(s, user_id: int, manage_ids) -> set[int]:
    """Group ids the user administers: explicit web grants plus groups whose
    linked Discord guild they can manage. Drives which private library rows
    they may see."""
    from db import Group

    gids = {
        gid
        for (gid,) in s.query(GroupAdmin.group_id).filter(GroupAdmin.user_id == user_id).all()
    }
    if manage_ids:
        gids |= {
            gid
            for (gid,) in s.query(Group.group_id)
            .filter(Group.guild_id.in_([str(g) for g in manage_ids]))
            .all()
        }
    return gids


@event_admin_bp.get("/event-task-library")
async def list_task_library():
    """Task presets for the pickers: curated seeds and group-saved tasks.
    Read access for any signed-in group admin (or superadmin). Public rows
    are site-wide; private rows only show to admins of the owning group."""
    user_id = current_user_id()
    query = (request.args.get("query") or "").strip()
    type_filter = (request.args.get("type") or "").strip()
    if type_filter and type_filter not in EVENT_TASK_TYPES:
        abort_problem(
            422, "Invalid type",
            f"type must be one of {list(EVENT_TASK_TYPES)}.",
        )
    try:
        page = max(int(request.args.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1

    manage_ids = manageable_guild_ids(user_id)

    def _load():
        from sqlalchemy import or_ as sa_or

        with db_session() as s:
            user = load_user(s, user_id)
            from web_api.deps import is_moderator

            superadmin = is_moderator(user)  # staff view: all rows
            admin_gids: set[int] = set()
            if not superadmin:
                admin_gids = _admin_group_ids(s, user_id, manage_ids)
                # "Any group admin": an explicit web grant or MANAGE_GUILD on
                # any linked guild qualifies.
                if not admin_gids and not manage_ids:
                    abort_problem(403, "Forbidden", "Event admins only.")
            q = s.query(EventTaskLibraryItem).filter(
                EventTaskLibraryItem.active.is_(True))
            if not superadmin:
                visible = [EventTaskLibraryItem.visibility == "public"]
                if admin_gids:
                    visible.append(EventTaskLibraryItem.group_id.in_(sorted(admin_gids)))
                q = q.filter(sa_or(*visible))
            if query:
                like = f"%{query}%"
                q = q.filter(EventTaskLibraryItem.name.like(like)
                             | EventTaskLibraryItem.description.like(like))
            if type_filter:
                q = q.filter(EventTaskLibraryItem.type == type_filter)
            rows = (q.order_by(EventTaskLibraryItem.name.asc())
                    .offset((page - 1) * _LIBRARY_PAGE_SIZE)
                    .limit(_LIBRARY_PAGE_SIZE)
                    .all())
            return [_library_row(r) for r in rows]

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


def _library_row(r: EventTaskLibraryItem) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "type": r.type,
        "target": r.target,
        "target_value": r.target_value,
        "default_points": int(r.default_points or 0),
        "difficulty": r.difficulty,
        "config": r.config,
        "source": r.source,
        "group_id": r.group_id,
        "visibility": r.visibility or "public",
    }


# --------------------------------------------------------------------------- #
# Task-library management (superadmin CP)
# --------------------------------------------------------------------------- #
# The library's read side above is shared with every group admin; writes are
# site-staff only — curated presets and globally-public rows shape every
# clan's pickers, so they're managed from /admin. Goal fields go through the
# same validate_task_payload as event tasks: a preset that saves is a preset
# that instantiates.

_LIBRARY_DIFFICULTIES = ("air", "water", "earth", "fire")


def _assert_library_admin(s, user_id: int) -> None:
    # Moderators (and superadmins) manage the curated library; every
    # create/update/delete below writes an AuditLog row with the actor.
    from web_api.deps import assert_moderator

    assert_moderator(load_user(s, user_id))


def _clean_library_name(body: dict, *, required: bool) -> str | None:
    if "name" not in body and not required:
        return None
    name = (str(body.get("name") or "")).strip()
    if not (1 <= len(name) <= 120):
        abort_problem(422, "Invalid name", "Preset name must be 1–120 characters.")
    return name


def _clean_library_fields(body: dict) -> dict:
    """Validated non-goal fields present in ``body`` (absent = unchanged)."""
    out: dict = {}
    if "description" in body:
        description = body.get("description")
        if description is not None and not isinstance(description, str):
            abort_problem(422, "Invalid description", "description must be a string or null.")
        out["description"] = (description or "").strip()[:2000] or None
    if "default_points" in body:
        points = body.get("default_points")
        if not isinstance(points, int) or isinstance(points, bool) or points < 0:
            abort_problem(422, "Invalid default_points",
                          "'default_points' must be a non-negative integer.")
        out["default_points"] = points
    if "difficulty" in body:
        difficulty = body.get("difficulty")
        if difficulty is not None and difficulty not in _LIBRARY_DIFFICULTIES:
            abort_problem(422, "Invalid difficulty",
                          f"difficulty must be one of {list(_LIBRARY_DIFFICULTIES)} or null.")
        out["difficulty"] = difficulty
    visibility = clean_task_visibility(body, default=None)
    if visibility is not None:
        out["visibility"] = visibility
    return out


def _validated_library_goal(s, ttype: str, body: dict, row: EventTaskLibraryItem | None) -> dict:
    """Merged + revalidated target/target_value/config for a library write."""
    from web_api.routes.event_task_validation import validate_task_payload

    target = body.get("target", row.target if row else None)
    if target is not None and not isinstance(target, str):
        abort_problem(422, "Invalid target", "'target' must be a string or null.")
    config = body.get("config", row.config if row else None)
    if config is not None and not isinstance(config, (str, dict)):
        abort_problem(422, "Invalid config", "'config' must be a JSON object, string or null.")
    normalized = validate_task_payload(s, {
        "type": ttype,
        "target": target,
        "target_value": body.get("target_value", row.target_value if row else None),
        "config": config,
    })
    return normalized


def _assert_library_name_free(s, name: str, *, source: str, group_id, exclude_id=None) -> None:
    """The (name, source, group_id) unique index, surfaced as a 409 instead of
    an opaque IntegrityError."""
    group_match = (
        EventTaskLibraryItem.group_id == group_id
        if group_id is not None
        else EventTaskLibraryItem.group_id.is_(None)
    )
    q = s.query(EventTaskLibraryItem.id).filter(
        EventTaskLibraryItem.source == source,
        group_match,
        EventTaskLibraryItem.name == name,
    )
    if exclude_id is not None:
        q = q.filter(EventTaskLibraryItem.id != exclude_id)
    if q.first() is not None:
        abort_problem(409, "Name taken", f"A preset named '{name}' already exists.")


@event_admin_bp.post("/event-task-library")
async def create_task_library_item():
    """Create a curated preset (site-wide, ``source='curated'``)."""
    user_id = current_user_id()
    body = await json_body()
    name = _clean_library_name(body, required=True)
    ttype = body.get("type")
    if ttype not in EVENT_TASK_TYPES:
        abort_problem(422, "Invalid task type", f"type must be one of {list(EVENT_TASK_TYPES)}.")
    fields = _clean_library_fields(body)

    def _apply():
        with db_session() as s:
            _assert_library_admin(s, user_id)
            normalized = _validated_library_goal(s, ttype, body, None)
            _assert_library_name_free(s, name, source="curated", group_id=None)
            row = EventTaskLibraryItem(
                name=name,
                type=ttype,
                target=normalized["target"],
                target_value=normalized["target_value"],
                config=normalized["config"],
                source="curated",
                group_id=None,
                default_points=fields.get("default_points", 0),
                description=fields.get("description"),
                difficulty=fields.get("difficulty"),
                visibility=fields.get("visibility", "public"),
                active=True,
            )
            s.add(row)
            s.flush()
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=None,
                action="event.library.create",
                target=f"web_event_task_library.{row.id}",
                before=None,
                after=json.dumps(_library_row(row)),
            ))
            s.commit()
            return _library_row(row)

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))


@event_admin_bp.patch("/event-task-library/<int:item_id>")
async def update_task_library_item(item_id: int):
    """Edit any preset — curated seeds and group-saved rows alike (absent keys
    are left unchanged). Changing goal fields (target/target_value/config, or
    type) revalidates the whole goal."""
    user_id = current_user_id()
    body = await json_body()
    name = _clean_library_name(body, required=False)
    ttype = body.get("type")
    if ttype is not None and ttype not in EVENT_TASK_TYPES:
        abort_problem(422, "Invalid task type", f"type must be one of {list(EVENT_TASK_TYPES)}.")
    fields = _clean_library_fields(body)

    def _apply():
        with db_session() as s:
            _assert_library_admin(s, user_id)
            row = (
                s.query(EventTaskLibraryItem)
                .filter(EventTaskLibraryItem.id == item_id,
                        EventTaskLibraryItem.active.is_(True))
                .first()
            )
            if row is None:
                abort_problem(404, "Preset not found", f"No task-library item {item_id}.")
            before = _library_row(row)
            if name is not None and name != row.name:
                _assert_library_name_free(
                    s, name, source=row.source, group_id=row.group_id, exclude_id=row.id,
                )
                row.name = name
            if ttype is not None or "target" in body or "target_value" in body or "config" in body:
                new_type = ttype or row.type
                normalized = _validated_library_goal(s, new_type, body, row)
                row.type = new_type
                row.target = normalized["target"]
                row.target_value = normalized["target_value"]
                row.config = normalized["config"]
            for key, value in fields.items():
                setattr(row, key, value)
            after = _library_row(row)
            if after != before:
                s.add(AuditLog(
                    actor_user_id=user_id,
                    group_id=row.group_id,
                    action="event.library.update",
                    target=f"web_event_task_library.{row.id}",
                    before=json.dumps(before),
                    after=json.dumps(after),
                ))
            s.commit()
            return after

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))


@event_admin_bp.delete("/event-task-library/<int:item_id>")
async def delete_task_library_item(item_id: int):
    """Soft-delete (active=false): the preset leaves every picker, but tasks
    already copied into events are their own rows and are untouched."""
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            _assert_library_admin(s, user_id)
            row = (
                s.query(EventTaskLibraryItem)
                .filter(EventTaskLibraryItem.id == item_id,
                        EventTaskLibraryItem.active.is_(True))
                .first()
            )
            if row is None:
                abort_problem(404, "Preset not found", f"No task-library item {item_id}.")
            row.active = False
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=row.group_id,
                action="event.library.delete",
                target=f"web_event_task_library.{row.id}",
                before=json.dumps(_library_row(row)),
                after=None,
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))
