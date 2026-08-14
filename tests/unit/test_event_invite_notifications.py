"""The invite endpoints must actually reach a human (web96a).

``tests/unit/test_event_clan_vs_clan.py`` silences notifications so it can
assert on roster mechanics against a scripted session. That leaves one thing
unproven and it is the whole feature: that
``POST /events/{id}/participants`` — and its bulk sibling, and accept/decline,
and remove — really do call the notifier, with the right row.

A silent invitation is the exact bug this work exists to fix, so it gets a
test that would fail if someone deleted the call.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import web_api.routes.event_participants as epr
import web_api.routes.events as evr
import services.event_invites as invites

from tests.unit.test_event_auth_modes import _S, _SessionCM, _event


def _cvc(**kw):
    kw.setdefault("mode", "clan_vs_clan")
    kw.setdefault("group_id", 10)
    return _event(**kw)


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


@pytest.fixture()
def calls(monkeypatch):
    """Record notifier calls instead of performing them."""
    seen = []
    for name in ("announce_invite", "announce_response", "announce_withdrawal"):
        monkeypatch.setattr(
            invites,
            name,
            (lambda n: lambda *a, **k: seen.append((n, k)))(name),
        )
    return seen


def _wire(monkeypatch, session, user_id=7):
    monkeypatch.setattr(epr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(epr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(epr, "_assert_event_admin", lambda *a, **k: None)
    monkeypatch.setattr(epr, "_assert_admin_of_group", lambda *a, **k: None)


class _RecEventGroup:
    """Stand-in EventGroup that behaves enough like a persisted row."""

    event_id = group_id = status = None  # class-level column stand-ins
    _next_id = 100

    def __init__(self, **kw):
        _RecEventGroup._next_id += 1
        self.id = _RecEventGroup._next_id
        for k, v in kw.items():
            setattr(self, k, v)


class TestInviteNotifies:
    async def test_single_invite_announces_the_new_row(
        self, client, monkeypatch, calls
    ):
        s = _S(
            [_cvc()],
            [SimpleNamespace(group_id=20, group_name="Clan B")],
            [],  # not already on the roster
        )
        _wire(monkeypatch, s)
        monkeypatch.setattr(epr, "EventGroup", _RecEventGroup)

        r = await client.post("/api/v1/events/1/participants", json={"group_id": 20})
        assert r.status_code == 200

        assert [name for name, _ in calls] == ["announce_invite"]
        kwargs = calls[0][1]
        assert kwargs["event_group"].group_id == 20
        assert kwargs["invited_group_name"] == "Clan B"
        assert kwargs["actor_user_id"] == 7

    async def test_a_rejected_invite_announces_nothing(
        self, client, monkeypatch, calls
    ):
        """409 on an existing roster row must not DM anyone a second time."""
        s = _S(
            [_cvc()],
            [SimpleNamespace(group_id=20, group_name="Clan B")],
            [SimpleNamespace(status="invited")],  # already on the roster
        )
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/participants", json={"group_id": 20})
        assert r.status_code == 409
        assert calls == []


class TestBulkInviteNotifies:
    async def test_one_announcement_per_newly_invited_clan(
        self, client, monkeypatch, calls
    ):
        """Each clan gets its OWN thread and DM fan-out — the multi-clan case
        must not collapse into one shared conversation."""
        real = epr.EventGroup  # reuse real columns so `.in_()` filters work

        class _Rec(_RecEventGroup):
            group_id = real.group_id
            event_id = real.event_id

        s = _S(
            [_cvc()],
            [],  # none already on the roster
            [(20, "Clan B"), (21, "Clan C")],  # id -> name lookup
        )
        _wire(monkeypatch, s)
        monkeypatch.setattr(epr, "EventGroup", _Rec)

        r = await client.post(
            "/api/v1/events/1/participants/bulk", json={"group_ids": [20, 21]}
        )
        assert r.status_code == 200
        assert [name for name, _ in calls] == ["announce_invite", "announce_invite"]
        invited = {k["event_group"].group_id for _, k in calls}
        assert invited == {20, 21}
        names = {k["invited_group_name"] for _, k in calls}
        assert names == {"Clan B", "Clan C"}

    async def test_skipped_clans_are_not_announced(self, client, monkeypatch, calls):
        real = epr.EventGroup

        class _Rec(_RecEventGroup):
            group_id = real.group_id
            event_id = real.event_id

        s = _S(
            [_cvc()],
            [(20,)],            # 20 already on the roster
            [(20, "Clan B"), (21, "Clan C")],
        )
        _wire(monkeypatch, s)
        monkeypatch.setattr(epr, "EventGroup", _Rec)

        r = await client.post(
            "/api/v1/events/1/participants/bulk", json={"group_ids": [20, 21]}
        )
        assert r.status_code == 200
        assert len(calls) == 1
        assert calls[0][1]["event_group"].group_id == 21


class TestResponseNotifies:
    def _row(self, status="invited"):
        return SimpleNamespace(
            id=55, group_id=20, role="opponent", status=status, responded_at=None,
            mirror_discord_event=False,
        )

    @pytest.mark.parametrize("action,accepted", [("accept", True), ("decline", False)])
    async def test_response_is_announced(
        self, client, monkeypatch, calls, action, accepted
    ):
        s = _S([_cvc()], [self._row()])
        _wire(monkeypatch, s)
        monkeypatch.setattr(epr, "_sync_event_guilds", lambda *a, **k: None)

        r = await client.post(f"/api/v1/events/1/participants/20/{action}")
        assert r.status_code == 200
        assert [name for name, _ in calls] == ["announce_response"]
        assert calls[0][1]["accepted"] is accepted
        assert calls[0][1]["event_group"].group_id == 20

    async def test_a_second_response_announces_nothing(
        self, client, monkeypatch, calls
    ):
        s = _S([_cvc()], [self._row(status="accepted")])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/participants/20/accept")
        assert r.status_code == 409
        assert calls == []


class TestRemovalNotifies:
    async def test_removal_announces_before_the_row_is_deleted(
        self, client, monkeypatch, calls
    ):
        row = SimpleNamespace(
            id=55, group_id=20, role="opponent", status="invited", responded_at=None
        )
        s = _S(
            [_cvc()],
            [row],   # the participant row
            [None],  # no teams bound to that clan
        )
        _wire(monkeypatch, s)
        monkeypatch.setattr(epr, "_sync_event_guilds", lambda *a, **k: None)

        r = await client.delete("/api/v1/events/1/participants/20")
        assert r.status_code == 200
        assert [name for name, _ in calls] == ["announce_withdrawal"]
        # The thread is anchored to this row's id, which is why the call has to
        # happen while the row still exists.
        assert calls[0][1]["event_group"].id == 55
