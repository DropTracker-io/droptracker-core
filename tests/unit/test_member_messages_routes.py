"""Members' own death messages — the website routes and the plugin endpoint.

The rules themselves are tested in test_member_messages.py; these pin the route
contracts around them: only your own accounts, a refused message is a 422 with
the rule's own wording and writes nothing, every mutation echoes the whole
payload, leaders can only review and block members of their own group, and the
plugin endpoint resolves the account by hash and speaks the same rules.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from quart import Quart

import web_api.routes.member_messages as routes

mm = sys.modules["db.member_messages"]

NOW = datetime(2026, 9, 14, 12, 0, 0)


class _Q:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0] if self.rows else None


class _R:
    def __init__(self, rows):
        self.rows = list(rows)

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    """Scripted: each query()/execute() takes the next batch, in order."""

    def __init__(self, queries=(), executes=()):
        self.queries = list(queries)
        self.executes = list(executes)
        self.added, self.deleted = [], []
        self.committed = self.rolled_back = False

    def query(self, *a, **k):
        assert self.queries, "unexpected query"
        return _Q(self.queries.pop(0))

    def execute(self, *a, **k):
        assert self.executes, "unexpected execute"
        return _R(self.executes.pop(0))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _CM:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


def _player(pid=9, name="Alice", user_id=7):
    return SimpleNamespace(player_id=pid, player_name=name, user_id=user_id)


def _wire(monkeypatch, session, *, admin=True, user_id=7, user=True):
    monkeypatch.setattr(routes, "current_user_id", lambda: user_id)
    monkeypatch.setattr(routes, "db_session", lambda: _CM(session))
    monkeypatch.setattr(routes, "manageable_guild_ids", lambda uid: set())
    monkeypatch.setattr(
        routes, "load_user",
        lambda s, uid: SimpleNamespace(user_id=uid, username="u") if user else None,
    )

    def _assert_admin(s, uid, gid, guilds, user=None):
        if not admin:
            routes.abort_problem(403, "Forbidden", "Admin rights on this group are required.")

    monkeypatch.setattr(routes, "assert_group_admin", _assert_admin)
    monkeypatch.setattr(routes, "AuditLog", lambda **kw: SimpleNamespace(kind="audit", **kw))


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


# ── The member's editor ─────────────────────────────────────────────────────

class TestMyDeathMessages:
    async def test_lists_each_account_with_its_messages_and_groups(self, client, monkeypatch):
        session = FakeSession(queries=[[_player(9, "Alice"), _player(10, "Iron Alice")]])
        _wire(monkeypatch, session)
        rows = {9: SimpleNamespace(messages='["{player_name} planked"]', updated_at=NOW)}
        monkeypatch.setattr(routes, "load_member_message_row", lambda s, pid, t: rows.get(pid))
        monkeypatch.setattr(
            routes, "death_message_group_status",
            lambda s, pid: [{"id": 5, "name": "Clan", "allowed": pid == 9, "blocked": False}],
        )
        r = await client.get("/api/v1/me/death-messages")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "private, no-store"
        body = await r.get_json()
        assert body["max_messages"] == 5 and body["max_length"] == 150
        assert body["tokens"][0]["token"] == "{player_name}"
        alice, iron = body["players"]
        assert alice == {
            "id": 9, "name": "Alice", "messages": ["{player_name} planked"],
            "updated_at": NOW.isoformat(),
            "groups": [{"id": 5, "name": "Clan", "allowed": True, "blocked": False}],
        }
        assert iron["messages"] == [] and iron["updated_at"] is None

    async def test_saves_and_echoes_the_payload(self, client, monkeypatch):
        session = FakeSession(queries=[[_player()], [_player()]])
        _wire(monkeypatch, session)
        saved = {}

        def _store(s, pid, messages, *, via, user_id=None, message_type=mm.MESSAGE_TYPE_DEATH):
            saved.update(pid=pid, messages=mm.normalize_messages(messages), via=via, user_id=user_id)
            return saved["messages"]

        monkeypatch.setattr(routes, "store_member_messages", _store)
        monkeypatch.setattr(
            routes, "load_member_message_row",
            lambda s, pid, t: SimpleNamespace(messages=json.dumps(saved["messages"]), updated_at=NOW),
        )
        monkeypatch.setattr(routes, "death_message_group_status", lambda s, pid: [])
        r = await client.put(
            "/api/v1/me/players/9/death-messages",
            json={"messages": ["  {player_name} forgot to eat ", ""]},
        )
        assert r.status_code == 200
        assert saved == {"pid": 9, "messages": ["{player_name} forgot to eat"], "via": "web", "user_id": 7}
        assert session.committed
        body = await r.get_json()
        assert body["players"][0]["messages"] == ["{player_name} forgot to eat"]

    async def test_a_refused_message_is_a_422_with_the_rules_wording(self, client, monkeypatch):
        session = FakeSession(queries=[[_player()]])
        _wire(monkeypatch, session)
        monkeypatch.setattr(mm, "load_member_message_row", lambda s, pid, t: None)
        r = await client.put(
            "/api/v1/me/players/9/death-messages", json={"messages": ["join discord.gg/scam"]},
        )
        assert r.status_code == 422
        body = await r.get_json()
        assert body["detail"] == "Messages can't contain links."
        assert not session.committed
        assert session.added == []

    async def test_someone_elses_account_is_a_404(self, client, monkeypatch):
        session = FakeSession(queries=[[]])
        _wire(monkeypatch, session)
        r = await client.put("/api/v1/me/players/99/death-messages", json={"messages": ["x"]})
        assert r.status_code == 404
        assert not session.committed

    async def test_messages_must_be_a_list(self, client, monkeypatch):
        _wire(monkeypatch, FakeSession())
        r = await client.put("/api/v1/me/players/9/death-messages", json={"messages": "hello"})
        assert r.status_code == 422

    async def test_unknown_session_is_a_401(self, client, monkeypatch):
        _wire(monkeypatch, FakeSession(), user=False)
        r = await client.get("/api/v1/me/death-messages")
        assert r.status_code == 401


# ── The leader's review ─────────────────────────────────────────────────────

def _group_rows():
    """execute() batches for _group_payload: members with messages, then blocks."""
    return [
        [
            (9, "Alice", '["{player_name} planked"]', NOW),
            (11, "Zed", '["https://scam.example.com"]', NOW),  # invalid today: hidden
        ],
        [(12, "Bob", NOW)],
    ]


class TestGroupReview:
    async def test_lists_members_with_messages_and_blocked_members(self, client, monkeypatch):
        session = FakeSession(executes=_group_rows())
        _wire(monkeypatch, session)
        monkeypatch.setattr(routes, "group_allows_member_death_messages", lambda s, gid: True)
        r = await client.get("/api/v1/groups/42/member-death-messages")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["enabled"] is True
        assert [m["name"] for m in body["members"]] == ["Alice", "Bob"]
        alice, bob = body["members"]
        assert alice["messages"] == ["{player_name} planked"] and alice["blocked"] is False
        assert bob["messages"] == [] and bob["blocked"] is True
        assert bob["blocked_at"] == NOW.isoformat()

    async def test_non_admin_is_refused(self, client, monkeypatch):
        _wire(monkeypatch, FakeSession(), admin=False)
        r = await client.get("/api/v1/groups/42/member-death-messages")
        assert r.status_code == 403

    async def test_block_adds_a_row_and_an_audit_entry(self, client, monkeypatch):
        # _member_name, then the existence check, then the echoed payload.
        session = FakeSession(
            queries=[[]],
            executes=[[("Alice",)]] + _group_rows(),
        )
        _wire(monkeypatch, session)
        monkeypatch.setattr(routes, "group_allows_member_death_messages", lambda s, gid: False)
        r = await client.put("/api/v1/groups/42/member-death-messages/9/block")
        assert r.status_code == 200
        assert session.committed
        audit = [a for a in session.added if getattr(a, "kind", None) == "audit"]
        assert len(audit) == 1
        assert audit[0].action == "member_messages.block"
        assert audit[0].after == "Alice" and audit[0].group_id == 42
        body = await r.get_json()
        assert body["enabled"] is False

    async def test_blocking_twice_is_a_no_op(self, client, monkeypatch):
        session = FakeSession(queries=[[1]], executes=[[("Alice",)]] + _group_rows())
        _wire(monkeypatch, session)
        monkeypatch.setattr(routes, "group_allows_member_death_messages", lambda s, gid: True)
        r = await client.put("/api/v1/groups/42/member-death-messages/9/block")
        assert r.status_code == 200
        assert session.added == [] and not session.committed

    async def test_only_members_of_this_group_can_be_blocked(self, client, monkeypatch):
        session = FakeSession(executes=[[]])
        _wire(monkeypatch, session)
        r = await client.put("/api/v1/groups/42/member-death-messages/9/block")
        assert r.status_code == 404
        assert session.added == []

    async def test_unblock_removes_the_row(self, client, monkeypatch):
        block = SimpleNamespace(id=3, group_id=42, player_id=9)
        session = FakeSession(queries=[[block], ["Alice"]], executes=_group_rows())
        _wire(monkeypatch, session)
        monkeypatch.setattr(routes, "group_allows_member_death_messages", lambda s, gid: True)
        r = await client.delete("/api/v1/groups/42/member-death-messages/9/block")
        assert r.status_code == 200
        assert session.deleted == [block]
        assert session.committed
        audit = [a for a in session.added if getattr(a, "kind", None) == "audit"]
        assert audit[0].action == "member_messages.unblock" and audit[0].before == "Alice"

    async def test_unblocking_someone_not_blocked_changes_nothing(self, client, monkeypatch):
        session = FakeSession(queries=[[]], executes=_group_rows())
        _wire(monkeypatch, session)
        monkeypatch.setattr(routes, "group_allows_member_death_messages", lambda s, gid: True)
        r = await client.delete("/api/v1/groups/42/member-death-messages/9/block")
        assert r.status_code == 200
        assert session.deleted == [] and not session.committed


# ── The plugin endpoint ─────────────────────────────────────────────────────

import api.routes.member_messages as plugin_routes  # noqa: E402


class _PluginSession(FakeSession):
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture()
def plugin_client():
    app = Quart(__name__)
    app.register_blueprint(plugin_routes.member_messages_bp)
    return app.test_client()


def _wire_plugin(monkeypatch, session):
    monkeypatch.setattr(plugin_routes, "get_db_session", lambda: session)
    monkeypatch.setattr(plugin_routes, "death_message_group_status", lambda s, pid: [
        {"id": 5, "name": "Clan", "allowed": True, "blocked": False},
    ])


class TestPluginEndpoint:
    async def test_reads_by_account_hash(self, plugin_client, monkeypatch):
        session = _PluginSession(queries=[[_player()]])
        _wire_plugin(monkeypatch, session)
        monkeypatch.setattr(
            plugin_routes, "load_member_message_row",
            lambda s, pid, t: SimpleNamespace(messages='["{player_name} planked"]'),
        )
        r = await plugin_client.get("/player/death_messages?player_name=Alice&acc_hash=123")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["messages"] == ["{player_name} planked"]
        assert body["player_name"] == "Alice"
        assert body["max_messages"] == 5
        assert body["groups"][0]["allowed"] is True
        assert session.closed

    async def test_unknown_account_is_a_404(self, plugin_client, monkeypatch):
        # Hash lookup, then name+hash lookup, both empty.
        session = _PluginSession(queries=[[], []])
        _wire_plugin(monkeypatch, session)
        r = await plugin_client.get("/player/death_messages?player_name=Nobody&acc_hash=1")
        assert r.status_code == 404

    async def test_hash_is_required(self, plugin_client, monkeypatch):
        _wire_plugin(monkeypatch, _PluginSession())
        r = await plugin_client.get("/player/death_messages?player_name=Alice")
        assert r.status_code == 400

    async def test_save_uses_the_shared_rules(self, plugin_client, monkeypatch):
        session = _PluginSession(queries=[[_player()]])
        _wire_plugin(monkeypatch, session)
        saved = {}

        def _store(s, pid, messages, *, via, user_id=None, message_type=mm.MESSAGE_TYPE_DEATH):
            saved.update(messages=mm.normalize_messages(messages), via=via, user_id=user_id)
            return saved["messages"]

        monkeypatch.setattr(plugin_routes, "store_member_messages", _store)
        monkeypatch.setattr(
            plugin_routes, "load_member_message_row",
            lambda s, pid, t: SimpleNamespace(messages=json.dumps(saved.get("messages", []))),
        )
        r = await plugin_client.post("/player/death_messages", json={
            "player_name": "Alice", "acc_hash": "123",
            "messages": ["{player_name} tried to tank {killer}"],
        })
        assert r.status_code == 200
        assert saved == {"messages": ["{player_name} tried to tank {killer}"], "via": "plugin", "user_id": None}
        assert session.committed
        assert (await r.get_json())["messages"] == ["{player_name} tried to tank {killer}"]

    async def test_a_refused_save_is_a_422_the_player_can_read(self, plugin_client, monkeypatch):
        session = _PluginSession(queries=[[_player()]])
        _wire_plugin(monkeypatch, session)
        monkeypatch.setattr(mm, "load_member_message_row", lambda s, pid, t: None)
        r = await plugin_client.post("/player/death_messages", json={
            "player_name": "Alice", "acc_hash": "123", "messages": ["@everyone lol"],
        })
        assert r.status_code == 422
        body = await r.get_json()
        assert body["error"] == "Messages can't mention people, roles or channels, or use custom emoji."
        assert not session.committed and session.rolled_back

    async def test_messages_must_be_a_list(self, plugin_client, monkeypatch):
        _wire_plugin(monkeypatch, _PluginSession())
        r = await plugin_client.post("/player/death_messages", json={
            "player_name": "Alice", "acc_hash": "123", "messages": "hello",
        })
        assert r.status_code == 422
