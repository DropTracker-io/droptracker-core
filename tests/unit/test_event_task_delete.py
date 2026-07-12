"""Task delete cascade (DELETE /events/{id}/tasks/{taskId}).

Template-instantiated events bind their board cells to the fresh task rows,
and ``web_event_bingo_cells`` / ``web_event_completions`` /
``web_event_progress`` all FK ``web_event_tasks.id`` with no cascade — a bare
``s.delete(task)`` used to trip an IntegrityError, making tasks on
template-created events undeletable. These pin the cascade: cells unbind (the
labeled cell survives), ledger/progress rows go, and points the task granted
come back off team scores.

Same scripted-session harness as ``test_event_team_edit_delete``: each
``_S(...)`` batch answers the next query in order, so an extra or missing
query is caught as a regression.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import web_api.routes.events as evr

from tests.unit.test_event_auth_modes import _S, _SessionCM, _event, _team


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, session, user_id=7):
    monkeypatch.setattr(evr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)


def _task(id=5, points=0, label="Kill Zulrah"):
    return SimpleNamespace(
        id=id, event_id=1, type="kc_target", label=label, points=points,
        target="Zulrah", target_value=10, requires_confirmation=False,
        visibility="public", config=None,
    )


class TestDeleteTaskCascade:
    async def test_unbound_zero_point_task_minimal_path(self, client, monkeypatch):
        # Query order: event, task, bonus-row scan, cell-id scan (none),
        # completions bulk delete, progress bulk delete. points=0 ⇒ no
        # completed-progress scan, no team loads.
        s = _S([_event()], [_task(points=0)], [], [], [], [])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/tasks/5")
        assert r.status_code == 200
        assert (await r.get_json())["ok"] is True
        assert s.committed
        assert s._batches == []
        # One audit row for the deletion.
        assert len(s.added) == 1

    async def test_bound_task_unwinds_cells_scores_and_ledger(self, client, monkeypatch):
        team = _team(4)
        team.score = 100
        progress = SimpleNamespace(team_id=4, completed=True)
        bonus = SimpleNamespace(team_id=4, quantity=3)
        # Query order: event, task, completed-progress scan, bonus-row scan,
        # team load, cell-id scan, bingo-completions bulk delete, cell unbind
        # (bulk update), completions bulk delete, progress bulk delete.
        s = _S(
            [_event()], [_task(points=10)], [progress], [bonus], [team],
            [(7,), (9,)], [], [], [], [],
        )
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/tasks/5")
        assert r.status_code == 200
        assert s.committed
        assert s._batches == []
        # 10 pts for the completed rollup + 3 pts of riding bonus taken back.
        assert team.score == 87
        assert len(s.added) == 1

    async def test_score_never_goes_negative(self, client, monkeypatch):
        team = _team(4)
        team.score = 5
        progress = SimpleNamespace(team_id=4, completed=True)
        s = _S([_event()], [_task(points=10)], [progress], [], [team], [], [], [])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/tasks/5")
        assert r.status_code == 200
        assert team.score == 0

    async def test_unknown_task_is_idempotent(self, client, monkeypatch):
        s = _S([_event()], [])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/tasks/5")
        assert r.status_code == 200
        assert not s.committed
        assert s.added == []

    async def test_unknown_event_404(self, client, monkeypatch):
        s = _S([])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/9/tasks/5")
        assert r.status_code == 404
        assert not s.committed
