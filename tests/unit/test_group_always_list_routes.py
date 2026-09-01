"""Group always-announce-list routes (the settings-page editor's backend).

The blacklist routes' twin (see test_group_blacklist_routes.py) — what matters
here is the same contract: what a leader adds is normalized with the *same*
rule the drop pipeline matches by, mutations echo the whole list payload, and
the cap / scoping / admin gates hold. Plus the one divergence: this list takes
no ``region`` entries, because a drop's value gate is the only gate it bypasses
and drops carry no region.

Same scripted-session harness as the other route tests.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.group_always_list as gal

from tests.unit.test_event_auth_modes import _S, _SessionCM

NOW = datetime(2026, 9, 1, 12, 0, 0)


class FakeRow:
    """Stands in for GroupNotificationAlwaysList (the real model is a MagicMock
    under the stubbed ``db`` package). Class attributes are the columns the
    filters compare against; instance attributes are the row's values."""

    id = MagicMock()
    group_id = MagicMock()
    entry_type = MagicMock()
    entry_name = MagicMock()
    match_key = MagicMock()

    def __init__(self, **kw):
        base = dict(
            id=1, group_id=42, entry_type="item",
            entry_name="Twisted ancestral colour kit",
            match_key="twisted-ancestral-colour-kit",
            game_id=24670, added_by_user_id=7, date_added=NOW,
        )
        base.update(kw)
        self.__dict__.update(base)


def _wire(monkeypatch, session, *, admin=True, user_id=7):
    monkeypatch.setattr(gal, "current_user_id", lambda: user_id)
    monkeypatch.setattr(gal, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(gal, "manageable_guild_ids", lambda uid: [])
    monkeypatch.setattr(
        gal, "load_user",
        lambda s, uid: SimpleNamespace(id=uid, username="owner", is_superadmin=True),
    )

    def _assert_admin(s, uid, gid, guilds, user=None):
        if not admin:
            gal.abort_problem(403, "Forbidden", "Not a group admin.")

    monkeypatch.setattr(gal, "assert_group_admin", _assert_admin)
    monkeypatch.setattr(gal, "GroupNotificationAlwaysList", FakeRow)
    monkeypatch.setattr(gal, "AuditLog", lambda **kw: SimpleNamespace(**kw))


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


# ── Read ────────────────────────────────────────────────────────────────────

class TestList:
    async def test_returns_entries_and_the_cap(self, client, monkeypatch):
        _wire(monkeypatch, _S([FakeRow(), FakeRow(id=2, entry_type="npc",
                                       entry_name="Zulrah", match_key="zulrah")]))
        r = await client.get("/api/v1/groups/42/notification-always-list")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["limit"] == gal.MAX_ENTRIES_PER_GROUP
        assert [e["name"] for e in body["entries"]] == [
            "Twisted ancestral colour kit", "Zulrah",
        ]
        assert body["entries"][0]["match_key"] == "twisted-ancestral-colour-kit"

    async def test_non_admin_is_refused(self, client, monkeypatch):
        _wire(monkeypatch, _S(), admin=False)
        r = await client.get("/api/v1/groups/42/notification-always-list")
        assert r.status_code == 403


# ── Add ─────────────────────────────────────────────────────────────────────

class TestAdd:
    async def test_stores_the_pipeline_match_key(self, client, monkeypatch):
        # The whole point: the leader's spelling is preserved for display, but
        # what gets stored for matching is the pipeline's normalized key.
        session = _S([], [], [FakeRow()])
        _wire(monkeypatch, session)
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": "Twisted_Ancestral Colour Kit",
                  "game_id": 24670},
        )
        assert r.status_code == 200
        added = [a for a in session.added if isinstance(a, FakeRow)]
        assert len(added) == 1
        assert added[0].entry_name == "Twisted_Ancestral Colour Kit"
        assert added[0].match_key == "twisted-ancestral-colour-kit"
        assert added[0].game_id == 24670
        assert added[0].added_by_user_id == 7
        assert session.committed

    async def test_npc_entries_use_the_npc_identity_rule(self, client, monkeypatch):
        session = _S([], [], [FakeRow(entry_type="npc", entry_name="The Whisperer",
                                      match_key="whisperer")])
        _wire(monkeypatch, session)
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "npc", "name": "The Whisperer"},
        )
        assert r.status_code == 200
        assert [a for a in session.added if isinstance(a, FakeRow)][0].match_key == "whisperer"

    async def test_region_entries_are_refused(self, client, monkeypatch):
        # The one deliberate divergence from the blacklist: this list bypasses
        # only the drop value gate, and drops carry no region.
        _wire(monkeypatch, _S())
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "region", "name": "Castle Wars"},
        )
        assert r.status_code == 422

    async def test_an_audit_row_records_who_listed_what(self, client, monkeypatch):
        session = _S([], [], [FakeRow()])
        _wire(monkeypatch, session)
        await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": "Twisted ancestral colour kit"},
        )
        audit = [a for a in session.added if not isinstance(a, FakeRow)]
        assert len(audit) == 1
        assert audit[0].action == "notification_always_list.add"
        assert audit[0].after == "Twisted ancestral colour kit"
        assert audit[0].actor_user_id == 7

    async def test_the_response_is_the_whole_list_payload(self, client, monkeypatch):
        # The editor replaces its state from this response instead of
        # re-fetching, and validates it against the same schema as the GET.
        session = _S([], [], [FakeRow()])
        _wire(monkeypatch, session)
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": "Twisted ancestral colour kit"},
        )
        body = await r.get_json()
        assert body["limit"] == gal.MAX_ENTRIES_PER_GROUP
        assert [e["name"] for e in body["entries"]] == ["Twisted ancestral colour kit"]
        assert body["entry"]["name"] == "Twisted ancestral colour kit"

    async def test_re_adding_an_existing_entry_is_a_no_op(self, client, monkeypatch):
        session = _S([FakeRow()], [FakeRow()])
        _wire(monkeypatch, session)
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": "twisted ancestral colour kit"},
        )
        assert r.status_code == 200
        assert not [a for a in session.added if isinstance(a, FakeRow)]

    @pytest.mark.parametrize(
        "body",
        [
            {"entry_type": "player", "name": "Bones"},
            {"entry_type": "", "name": "Bones"},
            {"name": "Bones"},
        ],
    )
    async def test_unknown_entry_type_is_refused(self, client, monkeypatch, body):
        _wire(monkeypatch, _S())
        r = await client.post("/api/v1/groups/42/notification-always-list", json=body)
        assert r.status_code == 422

    @pytest.mark.parametrize("name", ["", "   ", None, "???", "---"])
    async def test_unmatchable_names_are_refused(self, client, monkeypatch, name):
        # An empty match_key would match either everything or nothing depending
        # on the payload — refuse it at the door rather than store a landmine.
        _wire(monkeypatch, _S())
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": name},
        )
        assert r.status_code == 422

    async def test_unknown_npc_placeholder_is_refused(self, client, monkeypatch):
        # "Unknown" is what an unsourced submission carries; as an entry it
        # would force-announce every unsourced drop in the group.
        _wire(monkeypatch, _S())
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "npc", "name": "Unknown"},
        )
        assert r.status_code == 422

    async def test_too_long_a_name_is_refused(self, client, monkeypatch):
        _wire(monkeypatch, _S())
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": "x" * (gal.MAX_NAME_LENGTH + 1)},
        )
        assert r.status_code == 422

    async def test_non_integer_game_id_is_refused(self, client, monkeypatch):
        _wire(monkeypatch, _S())
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": "Bones", "game_id": "twenty"},
        )
        assert r.status_code == 422

    async def test_a_full_list_refuses_new_entries(self, client, monkeypatch):
        session = _S([], [FakeRow()] * gal.MAX_ENTRIES_PER_GROUP)
        _wire(monkeypatch, session)
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": "Bones"},
        )
        assert r.status_code == 409

    async def test_non_admin_cannot_add(self, client, monkeypatch):
        _wire(monkeypatch, _S(), admin=False)
        r = await client.post(
            "/api/v1/groups/42/notification-always-list",
            json={"entry_type": "item", "name": "Bones"},
        )
        assert r.status_code == 403


# ── Remove ──────────────────────────────────────────────────────────────────

class TestDelete:
    async def test_removes_the_entry_and_audits_it(self, client, monkeypatch):
        session = _S([FakeRow()], [])
        _wire(monkeypatch, session)
        r = await client.delete("/api/v1/groups/42/notification-always-list/1")
        assert r.status_code == 200
        audit = [a for a in session.added if not isinstance(a, FakeRow)]
        assert audit[0].action == "notification_always_list.remove"
        assert audit[0].before == "Twisted ancestral colour kit"
        assert session.committed

    async def test_the_response_is_the_whole_list_payload(self, client, monkeypatch):
        _wire(monkeypatch, _S([FakeRow()], []))
        r = await client.delete("/api/v1/groups/42/notification-always-list/1")
        body = await r.get_json()
        assert body["limit"] == gal.MAX_ENTRIES_PER_GROUP
        assert body["entries"] == []

    async def test_unknown_entry_is_a_404(self, client, monkeypatch):
        _wire(monkeypatch, _S([]))
        r = await client.delete("/api/v1/groups/42/notification-always-list/999")
        assert r.status_code == 404

    async def test_non_admin_cannot_remove(self, client, monkeypatch):
        _wire(monkeypatch, _S(), admin=False)
        r = await client.delete("/api/v1/groups/42/notification-always-list/1")
        assert r.status_code == 403
