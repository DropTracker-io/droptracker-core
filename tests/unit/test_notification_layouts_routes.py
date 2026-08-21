"""Notification component-layout routes (the builder's backend).

The interesting contract here is not CRUD, it is that the editor can only ever
save something the send path will honour: the same validator runs on save, the
same pilot allowlist gates writes, and a group outside the pilot is told so
rather than being handed an editor whose output would be ignored.

Same scripted-session harness as the other route tests.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.notification_layouts as nl

# Dotted import, not ``from services import component_layout``: conftest stubs
# the ``services`` package, so the attribute form yields a MagicMock.
from services.component_layout import MAX_BLOCKS, NOTIFICATION_TYPES

from tests.unit.test_event_auth_modes import _S, _SessionCM

NOW = datetime(2026, 8, 14, 12, 0, 0)

GOOD_BLOCKS = [
    {"type": "text", "content": "**{player_name}** did a thing"},
    {"type": "separator", "divider": True},
    {"type": "media", "urls": ["{image_url}"]},
]


class FakeRow:
    """Stands in for GroupComponentLayout (the real model is a MagicMock under
    the stubbed ``db`` package). The class attributes are the columns the
    filters compare against; the instance ones are the row's values."""

    group_id = MagicMock()
    notification_type = MagicMock()

    def __init__(self, **kw):
        base = dict(
            group_id=2, notification_type="pb",
            layout=json.dumps({"accent_color": "#c8aa6e", "blocks": GOOD_BLOCKS}),
            active=False, updated_at=NOW, created_at=NOW,
        )
        base.update(kw)
        self.__dict__.update(base)


def _wire(monkeypatch, session, *, admin=True, user_id=7):
    monkeypatch.setattr(nl, "current_user_id", lambda: user_id)
    monkeypatch.setattr(nl, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(nl, "manageable_guild_ids", lambda uid: [])
    monkeypatch.setattr(
        nl, "load_user",
        lambda s, uid: SimpleNamespace(id=uid, username="owner", is_superadmin=True),
    )

    def _assert_admin(s, uid, gid, guilds, user=None):
        if not admin:
            nl.abort_problem(403, "Forbidden", "Not a group admin.")

    monkeypatch.setattr(nl, "assert_group_admin", _assert_admin)
    monkeypatch.setattr(nl, "GroupComponentLayout", FakeRow)
    monkeypatch.setattr(nl, "AuditLog", lambda **kw: SimpleNamespace(**kw))


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


# ── Metadata ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_meta_documents_every_type(client, monkeypatch):
    monkeypatch.setattr(nl, "current_user_id", lambda: 7)
    resp = await client.get("/api/v1/notification-layouts/meta")
    assert resp.status_code == 200
    body = await resp.get_json()

    assert [t["key"] for t in body["types"]] == list(NOTIFICATION_TYPES)
    assert body["limits"]["max_blocks"] == MAX_BLOCKS
    # Every type has to offer the player's name and at least one image token,
    # or the editor cannot express the message people actually want.
    for t in body["types"]:
        tokens = {d["token"] for d in t["tokens"]}
        assert "player_name" in tokens
        assert "image_url" in tokens
        assert t["label"] and t["description"]


@pytest.mark.asyncio
async def test_meta_marks_frequently_absent_tokens_optional(client, monkeypatch):
    """The preview blanks these to show the sparse message; if they stopped
    being flagged the editor would promise images most players never send."""
    monkeypatch.setattr(nl, "current_user_id", lambda: 7)
    resp = await client.get("/api/v1/notification-layouts/meta")
    body = await resp.get_json()
    pb = next(t for t in body["types"] if t["key"] == "pb")
    optional = {d["token"] for d in pb["tokens"] if d["optional"]}
    assert {"image_url", "gear_image_url"} <= optional
    assert "personal_best" not in optional


# ── Listing ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_requires_group_admin(client, monkeypatch):
    _wire(monkeypatch, _S(), admin=False)
    resp = await client.get("/api/v1/groups/2/notification-layouts")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_reports_pilot_gate_rather_than_refusing(client, monkeypatch):
    batches = [[] for _ in NOTIFICATION_TYPES]
    _wire(monkeypatch, _S(*batches))
    resp = await client.get("/api/v1/groups/9999/notification-layouts")
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["enabled"] is False
    # Still serves the defaults, so a non-pilot admin sees what they would get.
    assert len(body["layouts"]) == len(NOTIFICATION_TYPES)
    assert all(l["custom"] is None and l["active"] is False for l in body["layouts"])
    assert all(l["default"]["blocks"] for l in body["layouts"])


@pytest.mark.asyncio
async def test_list_returns_saved_layout(client, monkeypatch):
    row = FakeRow(active=True)
    batches = [[row] for _ in NOTIFICATION_TYPES]
    _wire(monkeypatch, _S(*batches))
    resp = await client.get("/api/v1/groups/2/notification-layouts")
    body = await resp.get_json()
    assert body["enabled"] is True
    first = body["layouts"][0]
    assert first["custom"]["blocks"] == GOOD_BLOCKS
    assert first["custom"]["accent_color"] == "#c8aa6e"
    assert first["active"] is True
    assert first["updated_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_unparseable_row_reads_as_inactive(client, monkeypatch):
    """The send path ignores a row it cannot parse; the editor must agree, or
    it would show "live" for a type that is quietly sending embeds."""
    row = FakeRow(layout="{not json", active=True)
    batches = [[row] for _ in NOTIFICATION_TYPES]
    _wire(monkeypatch, _S(*batches))
    resp = await client.get("/api/v1/groups/2/notification-layouts")
    body = await resp.get_json()
    assert body["layouts"][0]["custom"] is None
    assert body["layouts"][0]["active"] is False


# ── Saving ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_put_rejects_unknown_type(client, monkeypatch):
    _wire(monkeypatch, _S())
    resp = await client.put(
        "/api/v1/groups/2/notification-layouts/lb", json={"blocks": GOOD_BLOCKS})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_refused_outside_the_pilot(client, monkeypatch):
    """Saving must be impossible where the send path would ignore the row."""
    _wire(monkeypatch, _S())
    resp = await client.put(
        "/api/v1/groups/9999/notification-layouts/pb", json={"blocks": GOOD_BLOCKS})
    assert resp.status_code == 403
    body = await resp.get_json()
    assert body["code"] == "components_pilot_only"


@pytest.mark.asyncio
async def test_put_rejects_a_layout_the_renderer_would_drop(client, monkeypatch):
    _wire(monkeypatch, _S())
    resp = await client.put(
        "/api/v1/groups/2/notification-layouts/pb",
        json={"blocks": [{"type": "text", "content": "   "}]})
    assert resp.status_code == 422
    body = await resp.get_json()
    assert "text" in body["detail"].lower()


@pytest.mark.asyncio
async def test_put_rejects_bad_accent(client, monkeypatch):
    _wire(monkeypatch, _S())
    resp = await client.put(
        "/api/v1/groups/2/notification-layouts/pb",
        json={"blocks": GOOD_BLOCKS, "accent_color": "rebeccapurple"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_creates_row_inactive_by_default(client, monkeypatch):
    """Authoring must not change what members receive until it is switched on."""
    s = _S([])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/groups/2/notification-layouts/pb",
        json={"blocks": GOOD_BLOCKS, "accent_color": "#c8aa6e"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["ok"] is True and body["active"] is False
    assert body["layout"]["blocks"] == GOOD_BLOCKS
    saved = next(o for o in s.added if isinstance(o, FakeRow))
    assert json.loads(saved.layout)["accent_color"] == "#c8aa6e"
    assert saved.active is False
    assert s.committed


@pytest.mark.asyncio
async def test_put_can_activate_an_existing_row(client, monkeypatch):
    row = FakeRow(active=False)
    s = _S([row])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/groups/2/notification-layouts/pb",
        json={"blocks": GOOD_BLOCKS, "active": True})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["active"] is True
    assert row.active is True
    # Audited, with the previous state, because this changes what every member
    # of the group receives.
    audit = next(o for o in s.added if getattr(o, "action", None) == "notification_layouts.update")
    assert json.loads(audit.before)["active"] is False
    assert json.loads(audit.after)["active"] is True


# ── Reverting ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_reverts_to_the_embed(client, monkeypatch):
    row = FakeRow(active=True)
    s = _S([row])
    _wire(monkeypatch, s)
    resp = await client.delete("/api/v1/groups/2/notification-layouts/pb")
    assert resp.status_code == 200
    assert s.committed
    audit = next(o for o in s.added if getattr(o, "action", None) == "notification_layouts.reset")
    assert audit.after is None


@pytest.mark.asyncio
async def test_delete_allowed_outside_the_pilot(client, monkeypatch):
    """A group dropped from the pilot must still be able to clear its rows."""
    s = _S([FakeRow(group_id=9999)])
    _wire(monkeypatch, s)
    resp = await client.delete("/api/v1/groups/9999/notification-layouts/pb")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_missing_row_is_not_an_error(client, monkeypatch):
    _wire(monkeypatch, _S([]))
    resp = await client.delete("/api/v1/groups/2/notification-layouts/pb")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_a_layout_that_no_longer_validates_reads_as_inactive(client, monkeypatch):
    """Saved layouts are validated, so this means Discord's limits moved under
    a stored template. The send path drops to the embed; the editor must not
    keep claiming the type is live."""
    row = FakeRow(
        layout=json.dumps({"blocks": [{"type": "text", "content": "x" * 100_000}]}),
        active=True,
    )
    batches = [[row] for _ in NOTIFICATION_TYPES]
    _wire(monkeypatch, _S(*batches))
    resp = await client.get("/api/v1/groups/2/notification-layouts")
    body = await resp.get_json()
    first = body["layouts"][0]
    # Still returned, so the admin can see and fix it — just not called live.
    assert first["custom"] is not None
    assert first["active"] is False
