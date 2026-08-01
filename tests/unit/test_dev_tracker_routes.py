"""Dev-tracker admin routes (superadmin project/task/note board).

Same scripted-session harness as the other route tests. The status tuples are
monkeypatched with real tuples (conftest stubs ``db`` as a MagicMock, and
``in`` checks against stubbed values fail silently — see CLAUDE.md), and
``_touch_project`` is stubbed because the scripted ``_Q`` fake has no
``update()``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.dev_tracker as devtrk

from tests.unit.test_event_auth_modes import _S, _SessionCM

NOW = datetime(2026, 8, 1, 12, 0, 0)
PROJECT_STATUSES = ("active", "completed", "archived")
TASK_STATUSES = ("planned", "in_progress", "blocked", "done")


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


class FakeProject:
    id = MagicMock()
    name = MagicMock()

    def __init__(self, **kw):
        base = dict(
            id=31, name="Loot sweep polish", description=None, status="active",
            completion_note=None, order=100, author="owner", created_at=NOW,
            updated_at=NOW, completed_at=None, tasks=[], notes=[],
        )
        base.update(kw)
        self.__dict__.update(base)


class FakeTask:
    id = MagicMock()
    project_id = MagicMock()

    def __init__(self, **kw):
        base = dict(
            id=71, project_id=31, title="Fix decay rounding", body_md=None,
            status="planned", completion_note=None, order=100, author="owner",
            created_at=NOW, updated_at=NOW, completed_at=None,
            subtasks=[], notes=[],
        )
        base.update(kw)
        self.__dict__.update(base)


class FakeSubtask:
    id = MagicMock()
    task_id = MagicMock()

    def __init__(self, **kw):
        base = dict(
            id=91, task_id=71, title="Write failing test", done=False,
            note=None, order=100, created_at=NOW, updated_at=NOW,
            completed_at=None,
        )
        base.update(kw)
        self.__dict__.update(base)


class FakeNote:
    id = MagicMock()

    def __init__(self, **kw):
        base = dict(
            id=51, project_id=31, task_id=None, body_md="a note",
            author="owner", created_at=NOW, updated_at=NOW,
        )
        base.update(kw)
        self.__dict__.update(base)


def _wire(monkeypatch, session, *, superadmin=True, user_id=7):
    monkeypatch.setattr(devtrk, "current_user_id", lambda: user_id)
    monkeypatch.setattr(devtrk, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(
        devtrk, "load_user",
        lambda s, uid: SimpleNamespace(
            id=uid, username="owner", is_superadmin=superadmin, is_moderator=False),
    )
    monkeypatch.setattr(devtrk, "PROJECT_STATUSES", PROJECT_STATUSES)
    monkeypatch.setattr(devtrk, "TASK_STATUSES", TASK_STATUSES)
    monkeypatch.setattr(devtrk, "DevProject", FakeProject)
    monkeypatch.setattr(devtrk, "DevTask", FakeTask)
    monkeypatch.setattr(devtrk, "DevSubtask", FakeSubtask)
    monkeypatch.setattr(devtrk, "DevNote", FakeNote)
    monkeypatch.setattr(devtrk, "_touch_project", lambda s, pid: None)
    monkeypatch.setattr(devtrk, "_audit", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_superadmin_forbidden(client, monkeypatch):
    _wire(monkeypatch, _S(), superadmin=False)
    resp = await client.get("/api/v1/admin/dev/projects")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_project_requires_name(client, monkeypatch):
    _wire(monkeypatch, _S())
    resp = await client.post("/api/v1/admin/dev/projects", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_project_rejects_bad_status(client, monkeypatch):
    _wire(monkeypatch, _S())
    resp = await client.post(
        "/api/v1/admin/dev/projects", json={"name": "X", "status": "bogus"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_project_ok(client, monkeypatch):
    s = _S()
    _wire(monkeypatch, s)
    resp = await client.post(
        "/api/v1/admin/dev/projects",
        json={"name": "  Recaps v2  ", "description": "phase 1"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["name"] == "Recaps v2"          # trimmed
    assert body["description"] == "phase 1"
    assert body["status"] == "active"
    assert body["author"] == "owner"            # from the acting user
    assert body["tasks"] == [] and body["notes"] == []
    assert len(s.added) == 1 and s.committed


@pytest.mark.asyncio
async def test_update_project_stamps_completion(client, monkeypatch):
    row = FakeProject(status="active", completed_at=None)
    _wire(monkeypatch, _S([row]))
    resp = await client.patch(
        "/api/v1/admin/dev/projects/31",
        json={"status": "completed", "completion_note": "shipped"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["status"] == "completed"
    assert body["completion_note"] == "shipped"
    assert body["completed_at"] is not None


@pytest.mark.asyncio
async def test_update_project_clears_completion_on_reopen(client, monkeypatch):
    row = FakeProject(status="completed", completed_at=NOW)
    _wire(monkeypatch, _S([row]))
    resp = await client.patch(
        "/api/v1/admin/dev/projects/31", json={"status": "active"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["status"] == "active"
    assert body["completed_at"] is None


@pytest.mark.asyncio
async def test_update_project_rejects_empty_patch(client, monkeypatch):
    _wire(monkeypatch, _S())
    resp = await client.patch("/api/v1/admin/dev/projects/31", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_project(client, monkeypatch):
    s = _S([FakeProject()])
    _wire(monkeypatch, s)
    resp = await client.delete("/api/v1/admin/dev/projects/31")
    assert resp.status_code == 200
    assert (await resp.get_json()) == {"ok": True}
    assert s.committed


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_task_missing_project_404(client, monkeypatch):
    _wire(monkeypatch, _S([]))
    resp = await client.post(
        "/api/v1/admin/dev/projects/999/tasks", json={"title": "T"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_task_ok(client, monkeypatch):
    s = _S([FakeProject()])
    _wire(monkeypatch, s)
    resp = await client.post(
        "/api/v1/admin/dev/projects/31/tasks",
        json={"title": "Fix rounding", "body_md": "plan text"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["title"] == "Fix rounding"
    assert body["status"] == "planned"
    assert body["completed_at"] is None
    assert body["subtasks"] == []
    assert len(s.added) == 1 and s.committed


@pytest.mark.asyncio
async def test_task_done_stamps_completion(client, monkeypatch):
    row = FakeTask(status="in_progress")
    _wire(monkeypatch, _S([row]))
    resp = await client.patch(
        "/api/v1/admin/dev/tasks/71",
        json={"status": "done", "completion_note": "merged"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["status"] == "done"
    assert body["completed_at"] is not None
    assert body["completion_note"] == "merged"


# ---------------------------------------------------------------------------
# Subtasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subtask_check_and_uncheck(client, monkeypatch):
    row = FakeSubtask(done=False)
    _wire(monkeypatch, _S([row], [FakeTask()]))
    resp = await client.patch(
        "/api/v1/admin/dev/subtasks/91", json={"done": True, "note": "repro'd"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["done"] is True
    assert body["note"] == "repro'd"
    assert body["completed_at"] is not None

    row2 = FakeSubtask(done=True, completed_at=NOW)
    _wire(monkeypatch, _S([row2], [FakeTask()]))
    resp = await client.patch("/api/v1/admin/dev/subtasks/91", json={"done": False})
    body = await resp.get_json()
    assert body["done"] is False
    assert body["completed_at"] is None


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_note_rejects_foreign_task(client, monkeypatch):
    _wire(monkeypatch, _S([FakeProject(id=31)], [FakeTask(project_id=999)]))
    resp = await client.post(
        "/api/v1/admin/dev/projects/31/notes",
        json={"body_md": "note", "task_id": 71})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_note_on_task_ok(client, monkeypatch):
    s = _S([FakeProject(id=31)], [FakeTask(id=71, project_id=31)])
    _wire(monkeypatch, s)
    resp = await client.post(
        "/api/v1/admin/dev/projects/31/notes",
        json={"body_md": "progress note", "task_id": 71})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["body_md"] == "progress note"
    assert body["task_id"] == 71
    assert body["author"] == "owner"
    assert len(s.added) == 1 and s.committed
