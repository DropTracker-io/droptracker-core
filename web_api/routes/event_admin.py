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

from db import (
    AuditLog,
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
    _load_event_or_404,
    _ts,
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
            if "target" in body or "target_value" in body:
                # Re-validate the merged goal per task type so an edit can't
                # leave a task the engine will never match.
                from web_api.routes.event_task_validation import validate_task_payload

                target = body.get("target", task.target)
                if target is not None and not isinstance(target, str):
                    abort_problem(422, "Invalid target", "'target' must be a string or null.")
                normalized = validate_task_payload(s, {
                    "type": task.type,
                    "target": target,
                    "target_value": body.get("target_value", task.target_value),
                    "config": task.config,
                })
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

    Creates tasks for library picks / inline new tasks, deletes auto-created
    tasks the new board no longer references, sets has_bingo/board_size, and
    — when the event is already live (implicit lifecycle) — grants free cells
    to every team immediately.
    """
    user_id = current_user_id()
    body = await json_body()
    size, cells_in = _validate_board_body(body)

    def _apply():
        from web_api.routes.event_task_validation import validate_task_payload

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)
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
            existing_tasks = {}
            if ref_ids:
                existing_tasks = {
                    t.id: t
                    for t in s.query(EventTask).filter(
                        EventTask.id.in_(ref_ids), EventTask.event_id == event_id
                    ).all()
                }
                missing = ref_ids - set(existing_tasks)
                if missing:
                    abort_problem(
                        404, "Task not found",
                        f"No task(s) {sorted(missing)} in this event.",
                    )

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
                    task = EventTask(
                        event_id=event_id,
                        type=preset.type,
                        label=preset.name,
                        target=preset.target,
                        target_value=preset.target_value,
                        points=points if points is not None else int(preset.default_points or 0),
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
                    normalized = validate_task_payload(s, nt)
                    task = EventTask(
                        event_id=event_id,
                        type=nt["type"],
                        label=nt["label"].strip()[:255],
                        target=normalized["target"],
                        target_value=normalized["target_value"],
                        points=points,
                        requires_confirmation=bool(nt.get("requires_confirmation")),
                        config=_merged_auto_config(normalized["config"]),
                    )
                    s.add(task)
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


@event_admin_bp.get("/event-task-library")
async def list_task_library():
    """Curated task presets for the designer picker. Read access for any
    signed-in group admin (or superadmin) — the library is site-wide, not
    scoped to one event."""
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
        with db_session() as s:
            user = load_user(s, user_id)
            if not is_superadmin(user):
                # "Any group admin": an explicit web grant or MANAGE_GUILD on
                # any linked guild qualifies.
                grant = (s.query(GroupAdmin.id)
                         .filter(GroupAdmin.user_id == user_id)
                         .first())
                if not grant and not manage_ids:
                    abort_problem(403, "Forbidden", "Event admins only.")
            q = s.query(EventTaskLibraryItem).filter(
                EventTaskLibraryItem.active.is_(True))
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
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "description": r.description,
                    "type": r.type,
                    "target": r.target,
                    "target_value": r.target_value,
                    "default_points": int(r.default_points or 0),
                    "difficulty": r.difficulty,
                    "config": r.config,
                }
                for r in rows
            ]

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))
