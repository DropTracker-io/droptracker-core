"""Hall of Fame layout routes (web_api/routes/hof_layouts.py).

What matters is that the editor can only save what the bot will honour — the
same validator runs on save and render, writes need the hall_of_fame
entitlement — and that the live preview reports a half-written layout as
errors rather than failing the request.

Same scripted-session harness as the notification layout route tests.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.hof_layouts as hr
from services.hof_layout import LAYOUT_TYPE, MAX_ROWS

from tests.unit.test_event_auth_modes import _S, _SessionCM

NOW = datetime(2026, 9, 23, 12, 0, 0)

GOOD_BLOCKS = [
    {"type": "section", "content": "## {boss_link}\n-# KC leader {top_kc_player}", "thumbnail": "{boss_image_url}"},
    {"type": "leaderboard", "board": "pb", "count": 3, "each_mode": True},
]


class FakeRow:
    group_id = MagicMock()
    notification_type = MagicMock()

    def __init__(self, **kw):
        base = dict(
            group_id=5, notification_type=LAYOUT_TYPE,
            layout=json.dumps({"accent_color": None, "blocks": GOOD_BLOCKS}),
            active=False, updated_at=NOW, created_at=NOW,
        )
        base.update(kw)
        self.__dict__.update(base)


def _wire(monkeypatch, session, *, admin=True, entitled=True):
    monkeypatch.setattr(hr, "current_user_id", lambda: 7)
    monkeypatch.setattr(hr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(hr, "manageable_guild_ids", lambda uid: [])
    monkeypatch.setattr(hr, "load_user", lambda s, uid: SimpleNamespace(id=uid, is_superadmin=False))

    def _assert_admin(s, uid, gid, guilds, user=None):
        if not admin:
            hr.abort_problem(403, "Forbidden", "Not a group admin.")

    monkeypatch.setattr(hr, "assert_group_admin", _assert_admin)
    monkeypatch.setattr(hr, "_entitled", lambda s, gid, user: entitled)
    monkeypatch.setattr(hr, "GroupComponentLayout", FakeRow)
    monkeypatch.setattr(hr, "AuditLog", lambda **kw: SimpleNamespace(**kw))


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


@pytest.mark.asyncio
async def test_meta_documents_boards_tokens_and_default(client, monkeypatch):
    monkeypatch.setattr(hr, "current_user_id", lambda: 7)
    resp = await client.get("/api/v1/hall-of-fame/layout/meta")
    assert resp.status_code == 200
    body = await resp.get_json()
    assert {b["key"] for b in body["boards"]} == {"pb", "kc", "loot_month", "loot_all"}
    tokens = {t["token"] for g in body["token_groups"] for t in g["tokens"]}
    assert {"boss_emoji", "coins_emoji", "top_kc_player", "top_looter_month"} <= tokens
    assert {t["token"] for t in body["row_tokens"]} >= {"medal", "player", "value"}
    assert body["limits"]["max_rows"] == MAX_ROWS
    assert body["default_layout"]["blocks"]
    assert isinstance(body["emojis"], list)


@pytest.mark.asyncio
async def test_get_requires_group_admin(client, monkeypatch):
    _wire(monkeypatch, _S(), admin=False)
    resp = await client.get("/api/v1/groups/5/hall-of-fame/layout")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_reports_saved_layout_and_gate(client, monkeypatch):
    row = FakeRow(active=True)
    _wire(monkeypatch, _S([row], []), entitled=False)
    resp = await client.get("/api/v1/groups/5/hall-of-fame/layout")
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["enabled"] is False
    assert body["custom"]["blocks"] == GOOD_BLOCKS
    assert body["active"] is True
    assert body["default"]["blocks"]
    assert body["bosses"] == [] and body["emoji_supported"] is False


@pytest.mark.asyncio
async def test_put_refused_without_the_entitlement(client, monkeypatch):
    _wire(monkeypatch, _S(), entitled=False)
    resp = await client.put("/api/v1/groups/5/hall-of-fame/layout", json={"blocks": GOOD_BLOCKS})
    assert resp.status_code == 403
    assert (await resp.get_json())["code"] == "hof_requires_upgrade"


@pytest.mark.asyncio
async def test_put_rejects_an_invalid_leaderboard(client, monkeypatch):
    _wire(monkeypatch, _S())
    resp = await client.put(
        "/api/v1/groups/5/hall-of-fame/layout",
        json={"blocks": [{"type": "leaderboard", "board": "kc", "count": 50}]},
    )
    assert resp.status_code == 422
    assert "between 1 and" in (await resp.get_json())["detail"]


@pytest.mark.asyncio
async def test_put_saves_draft_then_activates_with_audit(client, monkeypatch):
    s = _S([])
    _wire(monkeypatch, s)
    resp = await client.put("/api/v1/groups/5/hall-of-fame/layout", json={"blocks": GOOD_BLOCKS})
    assert resp.status_code == 200
    saved = next(o for o in s.added if isinstance(o, FakeRow))
    assert saved.active is False and saved.notification_type == LAYOUT_TYPE

    row = FakeRow(active=False)
    s = _S([row])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/groups/5/hall-of-fame/layout", json={"blocks": GOOD_BLOCKS, "active": True},
    )
    assert (await resp.get_json())["active"] is True and row.active is True
    audit = next(o for o in s.added if getattr(o, "action", None) == "hof_layout.update")
    assert json.loads(audit.before)["active"] is False


@pytest.mark.asyncio
async def test_preview_reports_errors_without_failing(client, monkeypatch):
    _wire(monkeypatch, _S())
    resp = await client.post(
        "/api/v1/groups/5/hall-of-fame/layout/preview",
        json={"blocks": [{"type": "text", "content": ""}]},
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["ok"] is False and body["errors"][0].startswith("Block 1")


@pytest.mark.asyncio
async def test_delete_audits_the_reset(client, monkeypatch):
    s = _S([FakeRow(active=True)])
    _wire(monkeypatch, s)
    resp = await client.delete("/api/v1/groups/5/hall-of-fame/layout")
    assert resp.status_code == 200
    audit = next(o for o in s.added if getattr(o, "action", None) == "hof_layout.reset")
    assert json.loads(audit.before)["active"] is True
