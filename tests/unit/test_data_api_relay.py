"""``/v2/relay/*`` (web153a): the endpoints a group's own Discord bot uses to
deliver DropTracker's messages. Driven through a real Quart app with the
relay blueprint, the real outbox core and an in-memory Redis."""
from __future__ import annotations

import asyncio

import pytest
from quart import Quart, g

from tests.unit.test_discord_relay import FakeRedis, _click

import data_api.routes.relay as relay_routes
from services import discord_relay_core as core

KEY_ID = 31
INTEGRATION = {
    "id": 7, "group_id": 299, "label": "TrueGritty", "application_id": "900",
    "status": "active", "features": ["events"], "hold_minutes": 360,
    "guild_ids": ["800"],
}


@pytest.fixture
def env(monkeypatch):
    r = FakeRedis()
    state = {"integration": dict(INTEGRATION)}
    relay_routes._integration_cache.clear()
    monkeypatch.setattr(relay_routes, "_redis", lambda: r)
    monkeypatch.setattr(relay_routes, "_load_integration",
                        lambda key_id: state["integration"] if key_id == KEY_ID else None)
    monkeypatch.setattr(relay_routes.usage, "record", lambda *a, **k: None)
    app = Quart(__name__)
    app.register_blueprint(relay_routes.relay_bp, url_prefix="/v2")

    @app.before_request
    async def _auth():
        g.api_key = {"key_id": state.get("key_id", KEY_ID)}

    return app, r, state


def _run(coro):
    return asyncio.run(coro)


async def _call(app, method, path, **kw):
    client = app.test_client()
    resp = await getattr(client, method)(path, **kw)
    return resp.status_code, await resp.get_json()


def test_unbound_key_is_forbidden(env):
    app, _r, state = env
    state["key_id"] = 999
    status, body = _run(_call(app, "get", "/v2/relay/integration"))
    assert status == 403 and body["error"] == "forbidden"


def test_integration_describes_the_rules(env):
    app, _r, _s = env
    status, body = _run(_call(app, "get", "/v2/relay/integration"))
    assert status == 200
    assert body["guild_ids"] == ["800"] and body["application_id"] == "900"
    assert {"prefix": "evtsignup:", "defer": "ephemeral"} in body["interaction_rules"]


def test_poll_ack_round_trip(env):
    app, r, _s = env
    mid = core.enqueue_create(r, 7, "111", {"content": "hello"},
                              [("board.png", b"PNG")], "test")
    status, body = _run(_call(app, "get", "/v2/relay/messages"))
    assert status == 200 and body["status"] == "active"
    [msg] = body["messages"]
    assert msg["id"] == mid and msg["files"][0]["url"] == f"/v2/relay/files/{mid}/0"

    client = app.test_client()

    async def fetch_file():
        resp = await client.get(f"/v2/relay/files/{mid}/0")
        return resp.status_code, await resp.get_data()

    assert _run(fetch_file()) == (200, b"PNG")
    status, body = _run(_call(app, "post", f"/v2/relay/messages/{mid}/ack",
                              json={"ok": True, "discord_message_id": "123"}))
    assert status == 200 and body == {"status": "sent"}
    assert core.load(r, mid)["discord_message_id"] == "123"


def test_ack_errors(env):
    app, r, _s = env
    status, _ = _run(_call(app, "post", "/v2/relay/messages/999/ack", json={"ok": True}))
    assert status == 404
    mid = core.enqueue_create(r, 7, "111", {"content": "x"})
    status, _ = _run(_call(app, "post", f"/v2/relay/messages/{mid}/ack",
                           json={"ok": True, "discord_message_id": "1"}))
    assert status == 409  # never claimed
    status, _ = _run(_call(app, "post", f"/v2/relay/messages/{mid}/ack", json={}))
    assert status == 400


def test_another_integrations_file_is_not_served(env):
    app, r, _s = env
    mid = core.enqueue_create(r, 8, "111", {"content": "x"}, [("a.png", b"1")])
    status, _ = _run(_call(app, "get", f"/v2/relay/files/{mid}/0"))
    assert status == 404


def test_paused_integration_receives_nothing(env):
    app, r, state = env
    state["integration"] = dict(INTEGRATION, status="paused")
    core.enqueue_create(r, 7, "111", {"content": "x"})
    status, body = _run(_call(app, "get", "/v2/relay/messages"))
    assert status == 200 and body == {"status": "paused", "messages": []}
    status, _ = _run(_call(app, "post", "/v2/relay/interactions", json=_click()))
    assert status == 409


def test_paused_still_honours_the_long_poll(env, monkeypatch):
    app, _r, state = env
    state["integration"] = dict(INTEGRATION, status="paused")
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(relay_routes.asyncio, "sleep", fake_sleep)
    status, body = _run(_call(app, "get", "/v2/relay/messages?wait=20"))
    assert status == 200 and body["status"] == "paused" and slept == [20]


def test_long_poll_returns_as_soon_as_something_arrives(env, monkeypatch):
    app, r, _s = env
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        core.enqueue_create(r, 7, "111", {"content": "late"})

    monkeypatch.setattr(relay_routes.asyncio, "sleep", fake_sleep)
    status, body = _run(_call(app, "get", "/v2/relay/messages?wait=20"))
    assert status == 200 and [m["body"] for m in body["messages"]] == [{"content": "late"}]
    assert len(sleeps) == 1


def test_interaction_is_validated_and_queued(env):
    app, r, _s = env
    status, body = _run(_call(app, "post", "/v2/relay/interactions", json=_click()))
    assert status == 202 and body == {"accepted": True, "defer": "ephemeral"}
    assert r.brpop(core.INTERACTION_QUEUE) is not None


@pytest.mark.parametrize("payload", [_click(app="901"), _click(guild="801"),
                                     _click("someone_elses:1")])
def test_bad_interactions_are_rejected(env, payload):
    app, r, _s = env
    status, body = _run(_call(app, "post", "/v2/relay/interactions", json=payload))
    assert status == 422 and body["error"] == "rejected"
    assert r.brpop(core.INTERACTION_QUEUE) is None


def test_channels_report_is_limited_to_owned_guilds(env):
    app, r, _s = env
    status, body = _run(_call(app, "put", "/v2/relay/channels",
                              json={"guild_id": "800", "channel_ids": ["1", "2"]}))
    assert status == 200 and body == {"remembered": 2}
    assert core.channel_guild(r, "1") == "800"
    status, _ = _run(_call(app, "put", "/v2/relay/channels",
                           json={"guild_id": "801", "channel_ids": ["3"]}))
    assert status == 403


def test_resync_queues_for_the_core_bot(env):
    app, r, _s = env
    status, _ = _run(_call(app, "post", "/v2/relay/resync", json={"event_id": 12}))
    assert status == 202
    assert b'"event_id": 12' in r.brpop(core.RESYNC_QUEUE)[1]
    status, _ = _run(_call(app, "post", "/v2/relay/resync", json={"event_id": "x"}))
    assert status == 400


def test_relay_ceiling(env, monkeypatch):
    app, _r, _s = env
    monkeypatch.setattr(relay_routes, "RELAY_REQUESTS_PER_MIN", 2)
    codes = [_run(_call(app, "get", "/v2/relay/integration"))[0] for _ in range(3)]
    assert codes == [200, 200, 429]
