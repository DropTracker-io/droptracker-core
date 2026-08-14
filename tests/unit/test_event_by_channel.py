"""GET /events/by-channel/<id> — the Activity's anonymous deep-link fallback.

Locks the launcher-channel exclusion: a channel hosting a group's standing
"Open DropTracker" card (``activity_launch_channel`` group config) must
resolve to NO event, even when an EventChannel row points at it — launches
from the card mean "open the app", not "open this channel's event".

Reuses the scripted-session harness from ``test_event_auth_modes``: each
``query()`` pops the next scripted batch, so the launcher-channel test also
proves the short-circuit (no EventChannel query is issued at all).
"""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta

import pytest

import web_api.routes.events as evr

from tests.unit.test_event_auth_modes import _S, _SessionCM


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, session):
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))


def test_channel_key_mirror_matches_core():
    # ACTIVITY_LAUNCH_CHANNEL_KEY is mirrored (not imported — the test harness
    # stubs the ``services`` package); this pins it to the real constant.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "services", "activity_launch_core.py")
    spec = importlib.util.spec_from_file_location("_alc_for_by_channel_test", path)
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    assert evr.ACTIVITY_LAUNCH_CHANNEL_KEY == core.CHANNEL_KEY


class TestEventByChannel:
    async def test_launcher_channel_never_resolves_an_event(self, client, monkeypatch):
        # Channel is some group's activity_launch_channel → null, and the
        # single scripted batch proves the EventChannel query never ran.
        s = _S([(42,)])
        _wire(monkeypatch, s)
        r = await client.get("/api/v1/events/by-channel/123456789")
        assert r.status_code == 200
        assert (await r.get_json()) == {"event_id": None}

    async def test_event_channel_resolves(self, client, monkeypatch):
        # Not a launcher channel; EventChannel row maps it to event 5.
        # Row shape: (id, status, starts_at, ends_at, ended_at).
        s = _S([], [(5, "active", None, None, None)])
        _wire(monkeypatch, s)
        r = await client.get("/api/v1/events/by-channel/123456789")
        assert r.status_code == 200
        assert (await r.get_json()) == {"event_id": 5}

    async def test_shared_channel_picks_the_nearer_event_not_the_newest(
        self, client, monkeypatch
    ):
        """Two drafts share one channel — the launch button must land on the
        one starting next, not on whichever was created last (the reported
        bug: a September event hijacked the running event's pot announcement).
        """
        now = datetime.now()
        s = _S([], [
            (46, "draft", now + timedelta(days=3), now + timedelta(days=18), None),
            (51, "draft", now + timedelta(days=28), now + timedelta(days=36), None),
        ])
        _wire(monkeypatch, s)
        r = await client.get("/api/v1/events/by-channel/123456789")
        assert r.status_code == 200
        assert (await r.get_json()) == {"event_id": 46}

    async def test_premature_end_is_measured_from_ended_at(self, client, monkeypatch):
        """A manually ended event finished when ``ended_at`` says, not when it
        was scheduled to — so it must not outrank an imminent draft."""
        now = datetime.now()
        s = _S([], [
            # Scheduled to run another 10 days; actually stopped 8 days ago.
            (60, "past", now - timedelta(days=20), now + timedelta(days=10),
             now - timedelta(days=8)),
            (61, "draft", now + timedelta(days=2), now + timedelta(days=9), None),
        ])
        _wire(monkeypatch, s)
        r = await client.get("/api/v1/events/by-channel/123456789")
        assert r.status_code == 200
        assert (await r.get_json()) == {"event_id": 61}

    async def test_unmapped_channel_is_null(self, client, monkeypatch):
        s = _S([], [])
        _wire(monkeypatch, s)
        r = await client.get("/api/v1/events/by-channel/123456789")
        assert r.status_code == 200
        assert (await r.get_json()) == {"event_id": None}

    async def test_non_digit_channel_short_circuits(self, client, monkeypatch):
        # No DB touch at all — zero scripted batches.
        s = _S()
        _wire(monkeypatch, s)
        r = await client.get("/api/v1/events/by-channel/not-a-channel")
        assert r.status_code == 200
        assert (await r.get_json()) == {"event_id": None}
