"""Internal dev tracker — the owner's project/task/note board (superadmin CMS).

Backs ``/admin/projects`` on the site: a lightweight in-house Trello for
tracking features (planned / in progress / done) with per-task subtask
checklists and free-form Markdown notes. Strictly superadmin-facing; codebase
agents use ``scripts/project_tracker.py`` against the same tables instead
(they have DB access but no Discord-OAuth session).

Hierarchy and rules live on the models (db/models/dev_tracker.py). Completion
is explicit at every level — checking all subtasks never auto-completes the
task — and every completable thing takes an optional completion note.

All endpoints are superadmin only:
  GET    /api/v1/admin/dev/projects                     -> ProjectSummary[]
  POST   /api/v1/admin/dev/projects                     -> ProjectDetail
  GET    /api/v1/admin/dev/projects/{id}                -> ProjectDetail
  PATCH  /api/v1/admin/dev/projects/{id}                -> ProjectDetail
  DELETE /api/v1/admin/dev/projects/{id}                -> { ok }
  POST   /api/v1/admin/dev/projects/{id}/tasks          -> Task
  PATCH  /api/v1/admin/dev/tasks/{id}                   -> Task
  DELETE /api/v1/admin/dev/tasks/{id}                   -> { ok }
  POST   /api/v1/admin/dev/tasks/{id}/subtasks          -> Subtask
  PATCH  /api/v1/admin/dev/subtasks/{id}                -> Subtask
  DELETE /api/v1/admin/dev/subtasks/{id}                -> { ok }
  POST   /api/v1/admin/dev/projects/{id}/notes          -> Note   (task_id optional)
  PATCH  /api/v1/admin/dev/notes/{id}                   -> Note
  DELETE /api/v1/admin/dev/notes/{id}                   -> { ok }
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from quart import Blueprint, jsonify
from sqlalchemy import func

from db import DevNote, DevProject, DevSubtask, DevTask, PROJECT_STATUSES, TASK_STATUSES
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import assert_superadmin, current_user_id, json_body, load_user

dev_tracker_bp = Blueprint("v1_dev_tracker", __name__)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _subtask(st: DevSubtask) -> dict:
    return {
        "id": st.id,
        "task_id": st.task_id,
        "title": st.title,
        "done": bool(st.done),
        "note": st.note,
        "order": st.order,
        "created_at": _iso(st.created_at),
        "completed_at": _iso(st.completed_at),
    }


def _note(n: DevNote) -> dict:
    return {
        "id": n.id,
        "project_id": n.project_id,
        "task_id": n.task_id,
        "body_md": n.body_md,
        "author": n.author,
        "created_at": _iso(n.created_at),
        "updated_at": _iso(n.updated_at),
    }


def _task(t: DevTask, *, include_children: bool = True) -> dict:
    out = {
        "id": t.id,
        "project_id": t.project_id,
        "title": t.title,
        "body_md": t.body_md,
        "status": t.status,
        "completion_note": t.completion_note,
        "order": t.order,
        "author": t.author,
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
        "completed_at": _iso(t.completed_at),
    }
    if include_children:
        out["subtasks"] = [_subtask(st) for st in t.subtasks]
        out["notes"] = [_note(n) for n in t.notes]
    return out


def _project_base(p: DevProject) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "completion_note": p.completion_note,
        "order": p.order,
        "author": p.author,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
        "completed_at": _iso(p.completed_at),
    }


def _project_detail(p: DevProject) -> dict:
    out = _project_base(p)
    out["tasks"] = [_task(t) for t in p.tasks]
    # Project-level notes only; task notes ride inside their task.
    out["notes"] = [_note(n) for n in p.notes if n.task_id is None]
    return out


# ---------------------------------------------------------------------------
# Validation (same shape as redirects.py: require_core=True on create,
# patch reads only supplied keys)
# ---------------------------------------------------------------------------

def _clean_str(value, *, field: str, limit: int, required: bool) -> str | None:
    text = str(value or "").strip()
    if not text:
        if required:
            abort_problem(422, "Missing field", f"'{field}' is required.")
        return None
    if len(text) > limit:
        abort_problem(422, "Invalid field", f"'{field}' must be {limit} characters or fewer.")
    return text


def _clean_status(value, allowed: tuple) -> str:
    status = str(value or "").strip()
    if status not in allowed:
        abort_problem(422, "Invalid status", f"Status must be one of: {', '.join(allowed)}.")
    return status


def _clean_order(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        abort_problem(422, "Invalid order", "'order' must be an integer.")


def _validate_project(body: dict, *, require_core: bool) -> dict:
    out: dict = {}
    if require_core or "name" in body:
        out["name"] = _clean_str(body.get("name"), field="name", limit=200, required=True)
    if "description" in body:
        out["description"] = (str(body.get("description") or "").strip()) or None
    if "status" in body:
        out["status"] = _clean_status(body["status"], PROJECT_STATUSES)
    if "completion_note" in body:
        out["completion_note"] = (str(body.get("completion_note") or "").strip()) or None
    if "order" in body:
        out["order"] = _clean_order(body["order"])
    return out


def _validate_task(body: dict, *, require_core: bool) -> dict:
    out: dict = {}
    if require_core or "title" in body:
        out["title"] = _clean_str(body.get("title"), field="title", limit=300, required=True)
    if "body_md" in body:
        out["body_md"] = (str(body.get("body_md") or "").strip()) or None
    if "status" in body:
        out["status"] = _clean_status(body["status"], TASK_STATUSES)
    if "completion_note" in body:
        out["completion_note"] = (str(body.get("completion_note") or "").strip()) or None
    if "order" in body:
        out["order"] = _clean_order(body["order"])
    return out


def _validate_subtask(body: dict, *, require_core: bool) -> dict:
    out: dict = {}
    if require_core or "title" in body:
        out["title"] = _clean_str(body.get("title"), field="title", limit=300, required=True)
    if "done" in body:
        out["done"] = bool(body["done"])
    if "note" in body:
        note = (str(body.get("note") or "").strip())[:500]
        out["note"] = note or None
    if "order" in body:
        out["order"] = _clean_order(body["order"])
    return out


def _validate_note(body: dict, *, require_core: bool) -> dict:
    out: dict = {}
    if require_core or "body_md" in body:
        out["body_md"] = _clean_str(body.get("body_md"), field="body_md", limit=65000, required=True)
    return out


# ---------------------------------------------------------------------------
# Shared bits
# ---------------------------------------------------------------------------

def _require_superadmin(s, actor: int) -> str:
    """Assert superadmin and return the author label written to new rows."""
    user = load_user(s, actor)
    assert_superadmin(user)
    username = getattr(user, "username", None) if user else None
    return (username or f"user:{actor}")[:80]


def _touch_project(s, project_id: int) -> None:
    """Bump the project's updated_at on child mutations so the list's
    "last activity" ordering stays honest."""
    s.query(DevProject).filter(DevProject.id == project_id).update(
        {"updated_at": datetime.now()}, synchronize_session=False
    )


def _get_or_404(s, model, row_id: int, label: str):
    row = s.query(model).filter(model.id == row_id).first()
    if not row:
        abort_problem(404, f"{label} not found", f"No {label.lower()} #{row_id}.")
    return row


def _apply_completion_stamp(row, fields: dict, done_value: str) -> None:
    """Maintain completed_at when a status field moves to/from the terminal
    value. Only acts when the patch actually carries a status change."""
    if "status" not in fields:
        return
    if fields["status"] == done_value and row.status != done_value:
        row.completed_at = datetime.now()
    elif fields["status"] != done_value and row.status == done_value:
        row.completed_at = None


def _audit(actor_user_id, action, target, before=None, after=None):
    try:
        from db import AuditLog

        with db_session() as s:
            s.add(AuditLog(
                actor_user_id=actor_user_id, group_id=None, action=action,
                target=target, before=before, after=after,
            ))
            s.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@dev_tracker_bp.get("/admin/dev/projects")
async def list_projects():
    actor = current_user_id()

    def _load():
        with db_session() as s:
            _require_superadmin(s, actor)
            projects = (
                s.query(DevProject)
                .order_by(DevProject.order.asc(), DevProject.updated_at.desc())
                .all()
            )
            # Aggregate child counts in three grouped queries instead of
            # walking each project's relationships (avoids N+1 lazy loads).
            task_counts: dict[int, dict] = {}
            for pid, status, count in (
                s.query(DevTask.project_id, DevTask.status, func.count(DevTask.id))
                .group_by(DevTask.project_id, DevTask.status)
                .all()
            ):
                bucket = task_counts.setdefault(pid, {"total": 0, "done": 0})
                bucket["total"] += count
                if status == "done":
                    bucket["done"] += count
            subtask_counts: dict[int, dict] = {}
            for pid, done, count in (
                s.query(DevTask.project_id, DevSubtask.done, func.count(DevSubtask.id))
                .join(DevTask, DevSubtask.task_id == DevTask.id)
                .group_by(DevTask.project_id, DevSubtask.done)
                .all()
            ):
                bucket = subtask_counts.setdefault(pid, {"total": 0, "done": 0})
                bucket["total"] += count
                if done:
                    bucket["done"] += count
            note_counts = dict(
                s.query(DevNote.project_id, func.count(DevNote.id))
                .group_by(DevNote.project_id)
                .all()
            )

            items = []
            for p in projects:
                row = _project_base(p)
                row["counts"] = {
                    "tasks_total": task_counts.get(p.id, {}).get("total", 0),
                    "tasks_done": task_counts.get(p.id, {}).get("done", 0),
                    "subtasks_total": subtask_counts.get(p.id, {}).get("total", 0),
                    "subtasks_done": subtask_counts.get(p.id, {}).get("done", 0),
                    "notes": note_counts.get(p.id, 0),
                }
                items.append(row)
            return items

    items = await asyncio.to_thread(_load)
    return private_no_store(jsonify(items))


@dev_tracker_bp.post("/admin/dev/projects")
async def create_project():
    actor = current_user_id()
    body = await json_body()
    fields = _validate_project(body, require_core=True)
    fields.setdefault("status", "active")

    def _create():
        with db_session() as s:
            author = _require_superadmin(s, actor)
            p = DevProject(author=author, **fields)
            s.add(p)
            s.commit()
            return _project_detail(p)

    payload = await asyncio.to_thread(_create)
    _audit(actor, "devtracker.project.create", f"dev_projects:{payload['id']}", after=fields["name"])
    return private_no_store(jsonify(payload))


@dev_tracker_bp.get("/admin/dev/projects/<int:project_id>")
async def get_project(project_id: int):
    actor = current_user_id()

    def _load():
        with db_session() as s:
            _require_superadmin(s, actor)
            p = _get_or_404(s, DevProject, project_id, "Project")
            return _project_detail(p)

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@dev_tracker_bp.patch("/admin/dev/projects/<int:project_id>")
async def update_project(project_id: int):
    actor = current_user_id()
    body = await json_body()
    fields = _validate_project(body, require_core=False)
    if not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")

    def _apply():
        with db_session() as s:
            _require_superadmin(s, actor)
            p = _get_or_404(s, DevProject, project_id, "Project")
            _apply_completion_stamp(p, fields, "completed")
            for k, v in fields.items():
                setattr(p, k, v)
            s.commit()
            return _project_detail(p)

    payload = await asyncio.to_thread(_apply)
    _audit(actor, "devtracker.project.update", f"dev_projects:{project_id}",
           after=str(sorted(fields.keys())))
    return private_no_store(jsonify(payload))


@dev_tracker_bp.delete("/admin/dev/projects/<int:project_id>")
async def delete_project(project_id: int):
    actor = current_user_id()

    def _delete():
        with db_session() as s:
            _require_superadmin(s, actor)
            p = _get_or_404(s, DevProject, project_id, "Project")
            name = p.name
            s.delete(p)
            s.commit()
            return name

    name = await asyncio.to_thread(_delete)
    _audit(actor, "devtracker.project.delete", f"dev_projects:{project_id}", before=name)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@dev_tracker_bp.post("/admin/dev/projects/<int:project_id>/tasks")
async def create_task(project_id: int):
    actor = current_user_id()
    body = await json_body()
    fields = _validate_task(body, require_core=True)
    fields.setdefault("status", "planned")

    def _create():
        with db_session() as s:
            author = _require_superadmin(s, actor)
            _get_or_404(s, DevProject, project_id, "Project")
            t = DevTask(project_id=project_id, author=author, **fields)
            if t.status == "done":
                t.completed_at = datetime.now()
            s.add(t)
            _touch_project(s, project_id)
            s.commit()
            return _task(t)

    payload = await asyncio.to_thread(_create)
    _audit(actor, "devtracker.task.create", f"dev_tasks:{payload['id']}", after=fields["title"])
    return private_no_store(jsonify(payload))


@dev_tracker_bp.patch("/admin/dev/tasks/<int:task_id>")
async def update_task(task_id: int):
    actor = current_user_id()
    body = await json_body()
    fields = _validate_task(body, require_core=False)
    if not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")

    def _apply():
        with db_session() as s:
            _require_superadmin(s, actor)
            t = _get_or_404(s, DevTask, task_id, "Task")
            _apply_completion_stamp(t, fields, "done")
            for k, v in fields.items():
                setattr(t, k, v)
            _touch_project(s, t.project_id)
            s.commit()
            return _task(t)

    payload = await asyncio.to_thread(_apply)
    _audit(actor, "devtracker.task.update", f"dev_tasks:{task_id}",
           after=str(sorted(fields.keys())))
    return private_no_store(jsonify(payload))


@dev_tracker_bp.delete("/admin/dev/tasks/<int:task_id>")
async def delete_task(task_id: int):
    actor = current_user_id()

    def _delete():
        with db_session() as s:
            _require_superadmin(s, actor)
            t = _get_or_404(s, DevTask, task_id, "Task")
            title = t.title
            _touch_project(s, t.project_id)
            s.delete(t)
            s.commit()
            return title

    title = await asyncio.to_thread(_delete)
    _audit(actor, "devtracker.task.delete", f"dev_tasks:{task_id}", before=title)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Subtasks
# ---------------------------------------------------------------------------

@dev_tracker_bp.post("/admin/dev/tasks/<int:task_id>/subtasks")
async def create_subtask(task_id: int):
    actor = current_user_id()
    body = await json_body()
    fields = _validate_subtask(body, require_core=True)

    def _create():
        with db_session() as s:
            _require_superadmin(s, actor)
            t = _get_or_404(s, DevTask, task_id, "Task")
            st = DevSubtask(task_id=task_id, **fields)
            if st.done:
                st.completed_at = datetime.now()
            s.add(st)
            _touch_project(s, t.project_id)
            s.commit()
            return _subtask(st)

    payload = await asyncio.to_thread(_create)
    return private_no_store(jsonify(payload))


@dev_tracker_bp.patch("/admin/dev/subtasks/<int:subtask_id>")
async def update_subtask(subtask_id: int):
    actor = current_user_id()
    body = await json_body()
    fields = _validate_subtask(body, require_core=False)
    if not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")

    def _apply():
        with db_session() as s:
            _require_superadmin(s, actor)
            st = _get_or_404(s, DevSubtask, subtask_id, "Subtask")
            if "done" in fields and fields["done"] != bool(st.done):
                st.completed_at = datetime.now() if fields["done"] else None
            for k, v in fields.items():
                setattr(st, k, v)
            t = s.query(DevTask).filter(DevTask.id == st.task_id).first()
            if t:
                _touch_project(s, t.project_id)
            s.commit()
            return _subtask(st)

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))


@dev_tracker_bp.delete("/admin/dev/subtasks/<int:subtask_id>")
async def delete_subtask(subtask_id: int):
    actor = current_user_id()

    def _delete():
        with db_session() as s:
            _require_superadmin(s, actor)
            st = _get_or_404(s, DevSubtask, subtask_id, "Subtask")
            t = s.query(DevTask).filter(DevTask.id == st.task_id).first()
            if t:
                _touch_project(s, t.project_id)
            s.delete(st)
            s.commit()

    await asyncio.to_thread(_delete)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@dev_tracker_bp.post("/admin/dev/projects/<int:project_id>/notes")
async def create_note(project_id: int):
    actor = current_user_id()
    body = await json_body()
    fields = _validate_note(body, require_core=True)
    task_id = body.get("task_id")
    if task_id is not None:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            abort_problem(422, "Invalid task", "'task_id' must be an integer.")

    def _create():
        with db_session() as s:
            author = _require_superadmin(s, actor)
            _get_or_404(s, DevProject, project_id, "Project")
            resolved_task_id = None
            if task_id is not None:
                t = _get_or_404(s, DevTask, task_id, "Task")
                if t.project_id != project_id:
                    abort_problem(422, "Wrong project", "That task belongs to another project.")
                resolved_task_id = t.id
            n = DevNote(project_id=project_id, task_id=resolved_task_id,
                        author=author, **fields)
            s.add(n)
            _touch_project(s, project_id)
            s.commit()
            return _note(n)

    payload = await asyncio.to_thread(_create)
    return private_no_store(jsonify(payload))


@dev_tracker_bp.patch("/admin/dev/notes/<int:note_id>")
async def update_note(note_id: int):
    actor = current_user_id()
    body = await json_body()
    fields = _validate_note(body, require_core=False)
    if not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")

    def _apply():
        with db_session() as s:
            _require_superadmin(s, actor)
            n = _get_or_404(s, DevNote, note_id, "Note")
            for k, v in fields.items():
                setattr(n, k, v)
            _touch_project(s, n.project_id)
            s.commit()
            return _note(n)

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))


@dev_tracker_bp.delete("/admin/dev/notes/<int:note_id>")
async def delete_note(note_id: int):
    actor = current_user_id()

    def _delete():
        with db_session() as s:
            _require_superadmin(s, actor)
            n = _get_or_404(s, DevNote, note_id, "Note")
            _touch_project(s, n.project_id)
            s.delete(n)
            s.commit()

    await asyncio.to_thread(_delete)
    return jsonify({"ok": True})
