"""The superadmin mirror switch: /api/v1/admin/edge-mirror.

Three modes now (off / testers / all). The body shape from before modes
(``{"enabled": bool}``) must keep working through a deploy window where the
old web build is still serving, and nothing but a known mode may be written.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import web_api.routes.admin as admin_routes
from tests.unit import _tester_db as tdb
from tests.unit.test_edge_config import FakeRedis


@pytest.fixture()
def env(monkeypatch):
    ec = tdb.load("_edge_config_for_admin", "services", "edge_config.py")
    fake = FakeRedis()
    monkeypatch.setattr(ec, "RedisClient", lambda: SimpleNamespace(client=fake))
    monkeypatch.setattr(ec, "tester_summary",
                        lambda: {"users": 2, "accounts": 3, "key_configured": True})
    monkeypatch.setitem(sys.modules, "services.edge_config", ec)

    async def superadmin():
        return 1

    audits = []
    monkeypatch.setattr(admin_routes, "_require_superadmin", superadmin)
    monkeypatch.setattr(admin_routes, "_audit",
                        lambda actor, action, target, before=None, after=None:
                        audits.append((action, after)))
    import web_api

    return SimpleNamespace(client=web_api.create_app().test_client(), redis=fake,
                           audits=audits, ec=ec)


async def _post(env, body):
    response = await env.client.post("/api/v1/admin/edge-mirror", json=body)
    return response.status_code, await response.get_json()


class TestSetMode:
    async def test_testers_mode_with_no_expiry(self, env):
        status, body = await _post(env, {"mode": "testers", "ttl_seconds": None})
        assert status == 200
        assert (body["mode"], body["enabled"], body["expires_at"]) == ("testers", False, None)
        assert body["testers"] == {"users": 2, "accounts": 3, "key_configured": True}
        assert json.loads(env.redis.store[env.ec.MIRROR_KEY])["mode"] == "testers"
        assert env.audits == [("edge.mirror.toggle", "testers (no expiry)")]

    async def test_everyone_for_four_hours(self, env):
        status, body = await _post(env, {"mode": "all", "ttl_seconds": 4 * 3600})
        assert status == 200 and body["mode"] == "all" and body["enabled"] is True
        assert env.redis.ttls[env.ec.MIRROR_KEY] == 4 * 3600
        assert env.audits == [("edge.mirror.toggle", "all (4h)")]

    async def test_off(self, env):
        await _post(env, {"mode": "testers"})
        status, body = await _post(env, {"mode": "off"})
        assert status == 200 and body["mode"] == "off"
        assert env.ec.MIRROR_KEY not in env.redis.store

    async def test_the_body_from_before_modes_still_works(self, env):
        status, body = await _post(env, {"enabled": True, "ttl_seconds": 3600})
        assert status == 200 and body["mode"] == "all"
        status, body = await _post(env, {"enabled": False, "ttl_seconds": None})
        assert status == 200 and body["mode"] == "off"

    @pytest.mark.parametrize("payload", [
        {"mode": "everyone"},
        {"mode": None},
        {},
        {"enabled": "yes"},
        {"mode": "all", "ttl_seconds": 123},
        {"mode": "testers", "ttl_seconds": True},
    ])
    async def test_anything_else_is_refused(self, env, payload):
        status, _ = await _post(env, payload)
        assert status == 422
        assert env.ec.MIRROR_KEY not in env.redis.store


class TestRead:
    async def test_reports_mode_and_testers(self, env):
        await _post(env, {"mode": "testers"})
        response = await env.client.get("/api/v1/admin/edge-mirror")
        body = await response.get_json()
        assert response.status_code == 200
        assert body["mode"] == "testers"
        assert body["testers"]["accounts"] == 3
