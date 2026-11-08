"""Manual sign-up add — POST /events/{id}/signups (the pool panel's "Add
players" box: pick from search, or paste a list).

Uses the scripted-session harness from ``test_event_auth_modes``: each batch
answers the next query the handler issues. ``_assert_event_admin`` is stubbed
(its contract is covered in the auth tests) and the service's decision is
stubbed (covered in test_event_signup_service.TestAddSignups); these pin the
body validation, name resolution, response shape and audit/commit.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import web_api.routes.events as evr

from tests.unit.test_event_auth_modes import _S, _SessionCM, _event

URL = "/api/v1/events/1/signups"


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


class _SignupError(Exception):
    def __init__(self, status, title, detail):
        super().__init__(detail)
        self.status, self.title, self.detail = status, title, detail


@pytest.fixture()
def service(monkeypatch):
    calls = []
    state = {"result": None, "raise": None}

    def add_signups(s, ev, players, *, scope_group_ids=None):
        calls.append((players, scope_group_ids))
        if state["raise"]:
            raise state["raise"]
        return state["result"] or {"added": [p[0] for p in players], "skipped": {}}

    fake = SimpleNamespace(add_signups=add_signups, SignupError=_SignupError)
    monkeypatch.setitem(sys.modules, "services.event_signup", fake)
    monkeypatch.setattr(sys.modules["services"], "event_signup", fake, raising=False)
    return SimpleNamespace(calls=calls, state=state)


def _wire(monkeypatch, session, user_id=7):
    monkeypatch.setattr(evr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)


def _pool_event():
    return _event(formation_mode="signup_pool")


class TestValidation:
    @pytest.mark.parametrize("body", [
        {},
        {"player_ids": [1], "names": ["a"]},
        {"player_ids": []},
        {"player_ids": ["3"]},
        {"player_ids": [True]},
        {"names": "zezima"},
        {"player_ids": [1], "group_id": "10"},
    ])
    async def test_bad_bodies_are_422(self, client, monkeypatch, service, body):
        _wire(monkeypatch, _S())
        res = await client.post(URL, json=body)
        assert res.status_code == 422
        assert service.calls == []


class TestAdd:
    async def test_picked_ids_are_added_and_audited(self, client, monkeypatch, service):
        s = _S([_pool_event()], [(3, "Zezima", 70), (4, "Lynx Titan", None)])
        _wire(monkeypatch, s)
        res = await client.post(URL, json={"player_ids": [3, 4, 3]})
        assert res.status_code == 200
        body = await res.get_json()
        assert body == {"added": [{"id": 3, "name": "Zezima"}, {"id": 4, "name": "Lynx Titan"}],
                        "skipped": []}
        assert service.calls == [([(3, "Zezima", 70), (4, "Lynx Titan", None)], None)]
        assert s.committed
        assert [a.action for a in s.added] == ["event.signup.manual_add"]

    async def test_skips_come_back_with_names_and_nothing_is_committed(
            self, client, monkeypatch, service):
        service.state["result"] = {"added": [], "skipped": {3: "Already signed up."}}
        s = _S([_pool_event()], [(3, "Zezima", None)])
        _wire(monkeypatch, s)
        res = await client.post(URL, json={"player_ids": [3, 9]})
        body = await res.get_json()
        assert body["added"] == []
        assert {"name": "Zezima", "reason": "Already signed up."} in body["skipped"]
        assert {"name": "Player 9", "reason": "No tracked player."} in body["skipped"]
        assert not s.committed and s.added == []

    async def test_pasted_names_resolve_through_the_folded_name(
            self, client, monkeypatch, service):
        # event, name resolve (folded), player rows
        s = _S([_pool_event()], [(5, "1 19")], [(5, "1 19", None)])
        _wire(monkeypatch, s)
        res = await client.post(URL, json={"names": ["1-19", "nobody here", "1_19"]})
        body = await res.get_json()
        assert body["added"] == [{"id": 5, "name": "1 19"}]
        assert body["skipped"] == [{"name": "nobody here",
                                    "reason": "No tracked player by that name."}]

    async def test_a_non_pool_event_is_refused(self, client, monkeypatch, service):
        service.state["raise"] = _SignupError(409, "Not a sign-up pool", "Add to a team.")
        _wire(monkeypatch, _S([_event(formation_mode="auto_assign")], [(3, "Z", None)]))
        res = await client.post(URL, json={"player_ids": [3]})
        assert res.status_code == 409

    async def test_past_event_blocked(self, client, monkeypatch, service):
        _wire(monkeypatch, _S([_event(formation_mode="signup_pool", status="past")]))
        res = await client.post(URL, json={"player_ids": [3]})
        assert res.status_code == 409
        assert service.calls == []
