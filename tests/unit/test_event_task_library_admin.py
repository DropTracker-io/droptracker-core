"""Task-library management routes (superadmin CP).

POST/PATCH/DELETE /api/v1/event-task-library[/{id}] — the write side of the
library is site-staff only (curated + globally-public presets shape every
clan's pickers). Same scripted-session harness as the other event route
tests; ``validate_task_payload`` is stubbed (its own contract is covered by
the task-validation tests).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.deps as deps
import web_api.routes.event_admin as evadm
import web_api.routes.event_task_validation as etv
import web_api.routes.events as evr
from web_api.common import ProblemException

from tests.unit.test_event_auth_modes import _S, _SessionCM


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


_TASK_TYPES = (
    "item_collection", "kc_target", "xp_target", "ehp_target", "ehb_target",
    "pb_target", "skill_target", "loot_value", "custom",
)


class FakeRow:
    """EventTaskLibraryItem stand-in: class-level mocks satisfy filter
    expressions, instance attrs hold real values for ``_library_row``."""

    id = MagicMock()
    name = MagicMock()
    source = MagicMock()
    group_id = MagicMock()
    active = MagicMock()

    def __init__(self, **kw):
        base = dict(
            id=11, name="Slay Zulrah", description=None, type="kc_target",
            target="Zulrah", target_value=10, default_points=5,
            difficulty=None, config=None, source="curated", group_id=None,
            visibility="public", active=True,
        )
        base.update(kw)
        self.__dict__.update(base)


def _wire(monkeypatch, session, *, superadmin=True, user_id=7):
    monkeypatch.setattr(evadm, "current_user_id", lambda: user_id)
    monkeypatch.setattr(evadm, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evadm, "load_user", lambda s, uid: SimpleNamespace(id=uid))
    monkeypatch.setattr(evadm, "EventTaskLibraryItem", FakeRow)
    monkeypatch.setattr(evadm, "EVENT_TASK_TYPES", _TASK_TYPES)
    # clean_task_visibility lives in the events module and reads this there.
    monkeypatch.setattr(evr, "EVENT_TASK_VISIBILITIES", ("public", "private"))
    monkeypatch.setattr(
        etv, "validate_task_payload",
        lambda s, body: {"target": (body.get("target") or "").strip() or None,
                         "target_value": body.get("target_value"),
                         "config": None},
    )
    if superadmin:
        monkeypatch.setattr(deps, "assert_superadmin", lambda user: None)
    else:
        def _deny(user):
            raise ProblemException(403, "Forbidden", "Site staff access is required.")

        monkeypatch.setattr(deps, "assert_superadmin", _deny)


class TestLibraryAdminAuth:
    async def test_create_requires_superadmin(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s, superadmin=False)
        r = await client.post(
            "/api/v1/event-task-library",
            json={"name": "X", "type": "kc_target", "target": "Zulrah", "target_value": 5},
        )
        assert r.status_code == 403
        assert not s.committed

    async def test_patch_requires_superadmin(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s, superadmin=False)
        r = await client.patch("/api/v1/event-task-library/11", json={"name": "Y"})
        assert r.status_code == 403

    async def test_delete_requires_superadmin(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s, superadmin=False)
        r = await client.delete("/api/v1/event-task-library/11")
        assert r.status_code == 403


class TestLibraryCreate:
    async def test_create_persists_curated_global_row(self, client, monkeypatch):
        # Query order: name-uniqueness probe (free).
        s = _S([])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/event-task-library",
            json={"name": "Slay Zulrah", "type": "kc_target",
                  "target": "Zulrah", "target_value": 10, "default_points": 5},
        )
        assert r.status_code == 200
        body = await r.get_json()
        assert body["name"] == "Slay Zulrah"
        assert body["source"] == "curated"
        assert body["group_id"] is None
        assert body["visibility"] == "public"
        assert s.committed
        # The row itself + one audit entry.
        assert len(s.added) == 2

    async def test_create_name_conflict_409(self, client, monkeypatch):
        s = _S([FakeRow()])  # uniqueness probe finds a row
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/event-task-library",
            json={"name": "Slay Zulrah", "type": "kc_target",
                  "target": "Zulrah", "target_value": 10},
        )
        assert r.status_code == 409
        assert not s.committed

    async def test_create_rejects_unknown_type(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/event-task-library", json={"name": "X", "type": "nope"},
        )
        assert r.status_code == 422


class TestLibraryUpdate:
    async def test_patch_updates_fields_and_audits(self, client, monkeypatch):
        row = FakeRow()
        # Query order: row load, name-uniqueness probe.
        s = _S([row], [])
        _wire(monkeypatch, s)
        r = await client.patch(
            "/api/v1/event-task-library/11",
            json={"name": "Zulrah speedrun", "default_points": 9, "visibility": "private"},
        )
        assert r.status_code == 200
        assert row.name == "Zulrah speedrun"
        assert row.default_points == 9
        assert row.visibility == "private"
        assert s.committed
        assert len(s.added) == 1  # audit row

    async def test_patch_goal_change_revalidates(self, client, monkeypatch):
        row = FakeRow()
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch(
            "/api/v1/event-task-library/11",
            json={"target": "Vorkath", "target_value": 25},
        )
        assert r.status_code == 200
        assert row.target == "Vorkath"
        assert row.target_value == 25

    async def test_patch_unknown_row_404(self, client, monkeypatch):
        s = _S([])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/event-task-library/11", json={"name": "Y"})
        assert r.status_code == 404

    async def test_patch_rejects_bad_difficulty(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/event-task-library/11", json={"difficulty": "hard"})
        assert r.status_code == 422


class TestLibraryDelete:
    async def test_delete_is_soft_and_audits(self, client, monkeypatch):
        row = FakeRow()
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/event-task-library/11")
        assert r.status_code == 200
        assert (await r.get_json())["ok"] is True
        assert row.active is False
        assert s.committed
        assert len(s.added) == 1  # audit row

    async def test_delete_unknown_row_404(self, client, monkeypatch):
        s = _S([])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/event-task-library/11")
        assert r.status_code == 404
