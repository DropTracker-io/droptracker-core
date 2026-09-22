"""/event_state withholds roster_version from plugins that rename chat nodes.

Plugin builds 6.0.3-6.0.8 badged event teammates in clan chat by renaming the
chat MessageNode, which wiped the node's sender identity: a friend's PMs were
hidden under Private: Friends and names in PMs were recoloured. Later builds
badge a line as it is drawn and say so with ``badges=2``. Every other client is
served no roster_version, which those builds read as "no badges" within one
poll, with no client restart and no Plugin Hub round-trip.

The service module is loaded from its file path because the conftest stubs the
``services`` package, and the route's lazy import is pointed at it per test.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest
from quart import Quart

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


pn = _load("_plugin_notifications_badge_gate", "services", "plugin_notifications.py")

import api.routes.notifications as routes  # noqa: E402

ROSTER_VERSION = "3f1c9a0b12345678"
STATE = {
    "events": [
        {"event": {"id": 46, "name": "Bingo"}, "roster_version": ROSTER_VERSION},
        {"event": {"id": 47, "name": "CvC"}, "roster_version": "0123456789abcdef"},
    ],
    "screenshot_item_ids": [11802],
}


class TestHelpers:
    def test_a_missing_or_unreadable_flag_is_a_legacy_plugin(self):
        for raw in (None, "", "abc", "2.0", "1"):
            assert pn.badge_renderer(raw) < pn.BADGE_RENDERER_DRAW_TIME

    def test_the_declared_renderer_is_read(self):
        assert pn.badge_renderer("2") == pn.BADGE_RENDERER_DRAW_TIME
        assert pn.badge_renderer("3") > pn.BADGE_RENDERER_DRAW_TIME

    def test_withholding_strips_every_roster_version_and_nothing_else(self):
        state = copy.deepcopy(STATE)
        assert pn.withhold_roster_versions(state) is state
        assert [e for e in state["events"] if "roster_version" in e] == []
        assert state["events"][0]["event"] == {"id": 46, "name": "Bingo"}
        assert state["screenshot_item_ids"] == [11802]

    def test_withholding_tolerates_odd_shapes(self):
        assert pn.withhold_roster_versions({}) == {}
        assert pn.withhold_roster_versions({"events": None}) == {"events": None}
        assert pn.withhold_roster_versions({"events": ["junk"]}) == {"events": ["junk"]}
        assert pn.withhold_roster_versions(None) is None


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.fixture()
def redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sys.modules["utils.redis"], "redis_client", SimpleNamespace(client=fake))
    return fake


@pytest.fixture()
def client(monkeypatch, redis):
    monkeypatch.setitem(sys.modules, "services.plugin_notifications", pn)
    monkeypatch.setattr(pn, "compose_event_state", lambda session, pid: copy.deepcopy(STATE))
    monkeypatch.setattr(routes, "get_db_session", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(routes, "_resolve_player_id", lambda session, name, acc_hash: 7)
    app = Quart(__name__)
    app.register_blueprint(routes.notifications_bp)
    return app.test_client()


async def _state(client, query=""):
    response = await client.get("/event_state?player_name=Zezima&acc_hash=123" + query)
    assert response.status_code == 200
    return await response.get_json()


class TestEventStateRoute:
    async def test_a_plugin_without_the_flag_gets_no_roster_version(self, client):
        body = await _state(client)
        assert [e for e in body["events"] if "roster_version" in e] == []
        # Everything else the HUD needs is still there.
        assert [e["event"]["id"] for e in body["events"]] == [46, 47]

    async def test_a_plugin_that_draws_badges_keeps_it(self, client):
        body = await _state(client, "&badges=2")
        assert body["events"][0]["roster_version"] == ROSTER_VERSION

    async def test_the_two_answers_are_cached_apart(self, client, redis):
        # A legacy answer cached first must never reach a new plugin, or its
        # badges would blink off for the cache's ten seconds at a time.
        await _state(client)
        body = await _state(client, "&badges=2")
        assert body["events"][0]["roster_version"] == ROSTER_VERSION
        assert len(redis.store) == 2
        # And served from the cache, each keeps its own answer.
        assert "roster_version" not in (await _state(client))["events"][0]
        assert (await _state(client, "&badges=2"))["events"][0]["roster_version"] == ROSTER_VERSION
