"""Site-wide notification defaults (the ACP's Default embeds page).

What matters here is not CRUD: it is that only staff can change what every
group without its own designs receives, that a default goes through the same
validation a group's own design does, that nothing can remove a default the
send path has no fallback for, that a component starting point can never go
live, and that the listing says truthfully who an edit will not reach.

Same scripted-session harness as the other route tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.embeds as embed_routes
import web_api.routes.event_layouts as event_layout_routes
import web_api.routes.notification_defaults as nd
import web_api.routes.notification_layouts as component_layout_routes

# Dotted import, not ``from services import component_layout``: conftest stubs
# the ``services`` package, so the attribute form yields a MagicMock.
from services.component_layout import NOTIFICATION_TYPES, default_layout

from tests.unit.test_event_auth_modes import _S, _SessionCM

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_event_layouts():
    """The real event layout service, by path: it is not registered under its
    dotted name, and the stubbed ``services`` package cannot import it."""
    path = os.path.join(_ROOT, "services", "event_message_layouts.py")
    spec = importlib.util.spec_from_file_location("_nd_event_message_layouts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ml = _load_event_layouts()

EVENT_TYPES = ("event_started", "event_ended")

GOOD_EMBED = {
    "title": "{item_name}",
    "description": "**{player_name}** received a drop",
    "color": "#c8aa6e",
    "fields": [{"name": "Value", "value": "{total_value}", "inline": True}],
}

GOOD_COMPONENT_BLOCKS = [
    {"type": "text", "content": "**{player_name}** did a thing"},
    {"type": "separator", "divider": True},
]

GOOD_EVENT_BLOCKS = [{"type": "text", "content": "## {event_name} has started"}]


class _Recorder(_S):
    """The scripted session, also remembering what was deleted."""

    def __init__(self, *batches):
        super().__init__(*batches)
        self.deleted = []

    def delete(self, obj):
        self.deleted.append(obj)


class FakeField:
    _next_id = 0

    def __init__(self, field_name, field_value, inline):
        FakeField._next_id += 1
        self.field_id = FakeField._next_id
        self.field_name = field_name
        self.field_value = field_value
        self.inline = inline


class FakeEmbed:
    """Stands in for GroupEmbed. Class attributes are the columns filters
    compare against; instance attributes are the row's values."""

    group_id = MagicMock()
    embed_type = MagicMock()
    embed_id = MagicMock()

    def __init__(self, **kw):
        base = dict(
            group_id=1, embed_type="drop", title="{item_name}", url=None,
            description="", color=None, thumbnail=None, image=None,
            timestamp=False,
        )
        base.update(kw)
        self.__dict__.update(base)
        self.fields = kw.get("fields", [])


class FakeEventRow:
    group_id = MagicMock()
    message_type = MagicMock()
    event_id = MagicMock()
    id = MagicMock()

    def __init__(self, **kw):
        base = dict(
            group_id=1, event_id=0, message_type="event_started",
            accent_color="#FFD700", layout=json.dumps({"blocks": GOOD_EVENT_BLOCKS}),
            schema_version=1,
        )
        base.update(kw)
        self.__dict__.update(base)


class FakeComponentRow:
    group_id = MagicMock()
    notification_type = MagicMock()

    def __init__(self, **kw):
        base = dict(
            group_id=1, notification_type="pb", active=False,
            layout=json.dumps({"accent_color": "#123456", "blocks": GOOD_COMPONENT_BLOCKS}),
        )
        base.update(kw)
        self.__dict__.update(base)


def _audit_entry(**kw):
    return SimpleNamespace(**kw)


def _wire(monkeypatch, session, *, superadmin=True, entitled=()):
    monkeypatch.setattr(nd, "current_user_id", lambda: 7)
    monkeypatch.setattr(nd, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(
        nd, "load_user",
        lambda s, uid: SimpleNamespace(id=uid, username="staff", is_superadmin=superadmin),
    )
    # The real lookup cannot run under the stubbed ``db`` package.
    monkeypatch.setattr(nd, "_entitled_group_ids", lambda ids: set(ids) & set(entitled))
    monkeypatch.setattr(nd, "_event_layouts_module", lambda: ml)
    monkeypatch.setattr(nd, "EVENT_MESSAGE_LAYOUT_TYPES", EVENT_TYPES)
    monkeypatch.setattr(nd, "AuditLog", _audit_entry)
    # The writers live in the group route modules and build their rows there.
    monkeypatch.setattr(embed_routes, "GroupEmbed", FakeEmbed)
    monkeypatch.setattr(embed_routes, "EmbedField", FakeField)
    monkeypatch.setattr(event_layout_routes, "EventMessageLayout", FakeEventRow)
    monkeypatch.setattr(event_layout_routes, "AuditLog", _audit_entry)
    monkeypatch.setattr(component_layout_routes, "GroupComponentLayout", FakeComponentRow)


def _audits(session):
    return [o for o in session.added if isinstance(o, SimpleNamespace)]


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


# ── Access ──────────────────────────────────────────────────────────────────

ALL_ROUTES = [
    ("get", "/embeds", None),
    ("put", "/embeds/drop", GOOD_EMBED),
    ("delete", "/embeds/quest", None),
    ("get", "/event-layouts", None),
    ("put", "/event-layouts/event_started", {"blocks": GOOD_EVENT_BLOCKS}),
    ("delete", "/event-layouts/event_started", None),
    ("get", "/component-layouts", None),
    ("put", "/component-layouts/pb", {"blocks": GOOD_COMPONENT_BLOCKS}),
    ("delete", "/component-layouts/pb", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path, body", ALL_ROUTES)
async def test_every_route_is_staff_only(client, monkeypatch, method, path, body):
    """These rows are what every group without its own designs is sent."""
    s = _Recorder()
    _wire(monkeypatch, s, superadmin=False)
    kwargs = {"json": body} if body is not None else {}
    resp = await getattr(client, method)(f"/api/v1/admin/notification-defaults{path}", **kwargs)
    assert resp.status_code == 403
    assert not s.added and not s.deleted and not s.committed


# ── Embed templates ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_embed_listing_reports_templates_builtins_and_who_keeps_their_own(client, monkeypatch):
    drop = FakeEmbed(embed_type="drop", title="{item_name}", color="#c8aa6e", fields=[
        FakeField("Value", "{total_value}", True),
    ])
    stray = FakeEmbed(embed_type="drop", title="stray duplicate")
    pairs = [("drop", 5), ("drop", 6), ("pet", 6)]
    s = _Recorder([drop, stray], pairs)
    # Group 6 is entitled, so its own drop and pet templates are sent instead.
    _wire(monkeypatch, s, entitled=(6,))
    resp = await client.get("/api/v1/admin/notification-defaults/embeds")
    assert resp.status_code == 200
    body = await resp.get_json()
    by_type = {e["embed_type"]: e for e in body["embeds"]}

    assert list(by_type) == list(embed_routes.EMBED_TYPES)
    # The first row wins, as the sender reads it.
    assert by_type["drop"]["template"]["title"] == "{item_name}"
    assert by_type["drop"]["template"]["fields"] == [
        {"name": "Value", "value": "{total_value}", "inline": True}]
    assert by_type["drop"]["builtin"] is None
    assert (by_type["drop"]["custom_count"], by_type["drop"]["override_count"]) == (2, 1)
    assert (by_type["pet"]["custom_count"], by_type["pet"]["override_count"]) == (1, 1)
    assert (by_type["clog"]["custom_count"], by_type["clog"]["override_count"]) == (0, 0)

    # Row-less types show the embed the sender builds instead.
    assert by_type["quest"]["template"] is None
    assert by_type["quest"]["builtin"]["title"] == "Quest Completed"
    assert {by_type[t]["builtin"] is not None for t in ("quest", "death", "diary")} == {True}


@pytest.mark.asyncio
async def test_embed_default_is_saved_on_the_template_group(client, monkeypatch):
    """No entitlement check: group 1 has no plan, and staff need none."""
    s = _Recorder([])
    _wire(monkeypatch, s)
    resp = await client.put("/api/v1/admin/notification-defaults/embeds/quest", json=GOOD_EMBED)
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["embed"]["title"] == "{item_name}"
    assert body["embed"]["fields"] == GOOD_EMBED["fields"]

    row = next(o for o in s.added if isinstance(o, FakeEmbed))
    assert row.group_id == 1 and row.embed_type == "quest"
    assert s.committed
    (audit,) = _audits(s)
    assert audit.action == "notification_defaults.embed.update"
    assert audit.target == "group_embeds.quest"
    assert audit.group_id is None
    assert audit.before is None
    assert json.loads(audit.after)["title"] == "{item_name}"


@pytest.mark.asyncio
async def test_embed_default_edit_rewrites_the_existing_row(client, monkeypatch):
    existing = FakeEmbed(embed_type="drop", title="Old title", fields=[
        FakeField("Old", "value", False),
    ])
    stray = FakeEmbed(embed_type="drop", title="duplicate")
    s = _Recorder([existing, stray])
    _wire(monkeypatch, s)
    resp = await client.put("/api/v1/admin/notification-defaults/embeds/drop", json=GOOD_EMBED)
    assert resp.status_code == 200
    assert existing.title == "{item_name}"
    assert [f.field_name for f in existing.fields] == ["Value"]
    assert s.deleted == [stray]
    (audit,) = _audits(s)
    assert json.loads(audit.before)["title"] == "Old title"


@pytest.mark.asyncio
async def test_embed_default_is_validated_like_a_group_template(client, monkeypatch):
    s = _Recorder([])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/admin/notification-defaults/embeds/drop",
        json={**GOOD_EMBED, "color": "rebeccapurple"})
    assert resp.status_code == 422
    assert not s.added and not s.committed


@pytest.mark.asyncio
async def test_an_untitled_default_can_be_saved_as_shipped(client, monkeypatch):
    """The combat achievement default has never had a title: its task line is
    the description. Staff must be able to edit it without inventing one."""
    s = _Recorder([])
    _wire(monkeypatch, s)
    body = {
        "title": "",
        "description": "{task_name} ({task_tier} - `{points_awarded}` points)",
        "fields": [{"name": "Current tier", "value": "{current_tier}", "inline": True}],
    }
    resp = await client.put("/api/v1/admin/notification-defaults/embeds/ca", json=body)
    assert resp.status_code == 200
    assert (await resp.get_json())["embed"]["title"] == ""


@pytest.mark.asyncio
async def test_an_embed_with_nothing_to_say_is_rejected(client, monkeypatch):
    s = _Recorder([])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/admin/notification-defaults/embeds/ca",
        json={"title": "  ", "description": "   ", "fields": []})
    assert resp.status_code == 422
    assert "title, a description or at least one field" in (await resp.get_json())["detail"]
    assert not s.added


@pytest.mark.asyncio
async def test_unknown_embed_type_is_rejected(client, monkeypatch):
    # "level" is a legacy row type the editor does not offer.
    _wire(monkeypatch, _Recorder())
    resp = await client.put("/api/v1/admin/notification-defaults/embeds/level", json=GOOD_EMBED)
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("embed_type", ["drop", "clog", "pb", "ca", "pet", "level_up", "lb"])
async def test_a_default_with_nothing_behind_it_cannot_be_removed(client, monkeypatch, embed_type):
    """Without the group-1 row these notifications fail to send at all."""
    s = _Recorder([FakeEmbed(embed_type=embed_type)])
    _wire(monkeypatch, s)
    resp = await client.delete(f"/api/v1/admin/notification-defaults/embeds/{embed_type}")
    assert resp.status_code == 422
    assert not s.deleted and not s.committed


@pytest.mark.asyncio
async def test_removing_a_builtin_types_default_restores_the_builtin(client, monkeypatch):
    rows = [FakeEmbed(embed_type="death", title="Custom death"), FakeEmbed(embed_type="death")]
    s = _Recorder(rows)
    _wire(monkeypatch, s)
    resp = await client.delete("/api/v1/admin/notification-defaults/embeds/death")
    assert resp.status_code == 200
    assert s.deleted == rows
    assert s.committed
    (audit,) = _audits(s)
    assert audit.action == "notification_defaults.embed.reset"
    assert json.loads(audit.before)["title"] == "Custom death"
    assert audit.after is None


@pytest.mark.asyncio
async def test_removing_a_missing_builtin_default_is_not_an_error(client, monkeypatch):
    s = _Recorder([])
    _wire(monkeypatch, s)
    resp = await client.delete("/api/v1/admin/notification-defaults/embeds/diary")
    assert resp.status_code == 200
    assert not s.committed


def test_builtin_embeds_are_valid_templates():
    """A built-in must be something the editor can save unchanged."""
    for embed_type, embed in nd.BUILTIN_EMBEDS.items():
        assert embed["embed_type"] == embed_type
        assert embed_routes._validate_body(embed)["title"] == embed["title"]


# ── Event message layouts ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_layout_listing(client, monkeypatch):
    started = FakeEventRow(message_type="event_started", accent_color="#00FF00")
    pairs = [("event_started", 5), ("event_started", 6), ("event_ended", 5)]
    s = _Recorder([started], pairs)
    _wire(monkeypatch, s, entitled=(6,))
    resp = await client.get("/api/v1/admin/notification-defaults/event-layouts")
    assert resp.status_code == 200
    body = await resp.get_json()
    by_type = {l["message_type"]: l for l in body["layouts"]}
    assert list(by_type) == list(EVENT_TYPES)

    assert by_type["event_started"]["template"] == {
        "message_type": "event_started", "accent_color": "#00FF00", "blocks": GOOD_EVENT_BLOCKS}
    assert (by_type["event_started"]["custom_count"], by_type["event_started"]["override_count"]) == (2, 1)
    # No row: the code default is what gets sent, and what the editor shows.
    assert by_type["event_ended"]["template"] is None
    assert by_type["event_ended"]["builtin"]["blocks"] == ml.DEFAULT_LAYOUTS["event_ended"]["blocks"]
    assert (by_type["event_ended"]["custom_count"], by_type["event_ended"]["override_count"]) == (1, 0)


@pytest.mark.asyncio
async def test_event_layout_default_is_saved_on_the_template_group(client, monkeypatch):
    s = _Recorder([])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/admin/notification-defaults/event-layouts/event_started",
        json={"blocks": GOOD_EVENT_BLOCKS, "accent_color": "#FFD700"},
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["layout"]["blocks"] == GOOD_EVENT_BLOCKS

    row = next(o for o in s.added if isinstance(o, FakeEventRow))
    assert (row.group_id, row.event_id, row.message_type) == (1, 0, "event_started")
    (audit,) = _audits(s)
    assert audit.action == "notification_defaults.event_layout.update"
    assert audit.group_id is None and audit.event_id is None


@pytest.mark.asyncio
async def test_event_layout_default_is_validated_like_a_group_layout(client, monkeypatch):
    s = _Recorder([])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/admin/notification-defaults/event-layouts/event_started",
        json={"blocks": [{"type": "standings", "limit": 999}]},
    )
    assert resp.status_code == 422
    assert not s.added


@pytest.mark.asyncio
async def test_unknown_event_type_is_rejected(client, monkeypatch):
    _wire(monkeypatch, _Recorder())
    resp = await client.put(
        "/api/v1/admin/notification-defaults/event-layouts/not_a_type",
        json={"blocks": GOOD_EVENT_BLOCKS},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_removing_an_event_layout_default_restores_the_builtin(client, monkeypatch):
    row = FakeEventRow()
    s = _Recorder([row])
    _wire(monkeypatch, s)
    resp = await client.delete("/api/v1/admin/notification-defaults/event-layouts/event_started")
    assert resp.status_code == 200
    assert s.deleted == [row] and s.committed
    (audit,) = _audits(s)
    assert audit.action == "notification_defaults.event_layout.reset"
    assert audit.target == "web_event_message_layouts.event_started"


# ── Component starting layouts ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_component_listing(client, monkeypatch):
    template = FakeComponentRow(group_id=1, notification_type="pb")
    others = [
        FakeComponentRow(group_id=5, notification_type="pb", active=True),
        FakeComponentRow(group_id=6, notification_type="pb", active=True),
        FakeComponentRow(group_id=7, notification_type="pb", active=False),
        # Active but unparseable: the sender ignores it, so it is not live.
        FakeComponentRow(group_id=8, notification_type="drop", active=True, layout="{nope"),
    ]
    s = _Recorder([template], others)
    _wire(monkeypatch, s, entitled=(5, 8))
    resp = await client.get("/api/v1/admin/notification-defaults/component-layouts")
    assert resp.status_code == 200
    body = await resp.get_json()
    by_type = {l["notification_type"]: l for l in body["layouts"]}
    assert list(by_type) == list(NOTIFICATION_TYPES)

    assert by_type["pb"]["template"] == {"accent_color": "#123456", "blocks": GOOD_COMPONENT_BLOCKS}
    assert by_type["pb"]["builtin"]["blocks"] == default_layout("pb")["blocks"]
    # Three saved; only group 5 is both entitled and switched over.
    assert (by_type["pb"]["custom_count"], by_type["pb"]["live_count"]) == (3, 1)
    assert (by_type["drop"]["custom_count"], by_type["drop"]["live_count"]) == (1, 0)
    assert by_type["drop"]["template"] is None


@pytest.mark.asyncio
async def test_component_default_never_goes_live(client, monkeypatch):
    """Group 1 sends nothing; a default is only ever a starting point."""
    s = _Recorder([])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/admin/notification-defaults/component-layouts/pb",
        json={"blocks": GOOD_COMPONENT_BLOCKS, "accent_color": "#c8aa6e", "active": True},
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body == {
        "ok": True,
        "notification_type": "pb",
        "layout": {"accent_color": "#c8aa6e", "blocks": GOOD_COMPONENT_BLOCKS},
        "active": False,
    }
    row = next(o for o in s.added if isinstance(o, FakeComponentRow))
    assert row.group_id == 1 and row.active is False
    (audit,) = _audits(s)
    assert audit.action == "notification_defaults.component_layout.update"
    assert audit.target == "group_component_layouts.pb"


@pytest.mark.asyncio
async def test_component_default_is_validated_like_a_group_layout(client, monkeypatch):
    s = _Recorder([])
    _wire(monkeypatch, s)
    resp = await client.put(
        "/api/v1/admin/notification-defaults/component-layouts/pb",
        json={"blocks": [{"type": "text", "content": "   "}]},
    )
    assert resp.status_code == 422
    assert not s.added


@pytest.mark.asyncio
async def test_removing_a_component_default_restores_the_builtin(client, monkeypatch):
    row = FakeComponentRow()
    s = _Recorder([row])
    _wire(monkeypatch, s)
    resp = await client.delete("/api/v1/admin/notification-defaults/component-layouts/pb")
    assert resp.status_code == 200
    assert s.deleted == [row] and s.committed
    (audit,) = _audits(s)
    assert audit.action == "notification_defaults.component_layout.reset"
    assert json.loads(audit.before)["blocks"] == GOOD_COMPONENT_BLOCKS
