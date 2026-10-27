"""The plugin's privacy switches, and "hidden" reaching the global Discord.

``/player/privacy`` reads and writes the four website switches on the Discord
user that owns the logged-in account, found the same way as
``/player/death_messages`` (acc_hash first). An account with no linked user
gets a 409 it can show, never a form that saves nowhere.

``player_hidden_globally`` is what makes "Hide me everywhere" mean the global
group's Discord posts too, while the player's own groups keep posting.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from quart import Quart

import api.routes.player_privacy as routes
from data.submissions import common


class _Q:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *a, **k):
        return self

    def outerjoin(self, *a, **k):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class _Session:
    """Scripted: each query() takes the next batch, in order."""

    def __init__(self, *queries):
        self.queries = list(queries)
        self.committed = self.rolled_back = self.closed = False

    def query(self, *a, **k):
        assert self.queries, "unexpected query"
        return _Q(self.queries.pop(0))

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _player(user_id=7):
    return SimpleNamespace(player_id=3, player_name="Alice", user_id=user_id)


def _user(**settings):
    values = {key: False for key in routes.PRIVACY_SETTINGS}
    values.update(settings)
    return SimpleNamespace(user_id=7, **values)


@pytest.fixture()
def client():
    app = Quart(__name__)
    app.register_blueprint(routes.player_privacy_bp)
    return app.test_client()


def _wire(monkeypatch, session):
    import api.routes.member_messages as mm_routes  # _resolve_player lives there
    monkeypatch.setattr(routes, "get_db_session", lambda: session)
    return mm_routes


class TestRead:
    async def test_reads_the_owning_users_switches(self, client, monkeypatch):
        session = _Session([_player()], [_user(hidden=True, never_ping=True)])
        _wire(monkeypatch, session)
        r = await client.get("/player/privacy?player_name=Alice&acc_hash=123")
        assert r.status_code == 200
        body = await r.get_json()
        assert body == {"player_name": "Alice", "settings": {
            "hidden": True, "global_ping": False, "group_ping": False, "never_ping": True}}
        assert session.closed

    async def test_unlinked_account_is_a_409_it_can_show(self, client, monkeypatch):
        _wire(monkeypatch, _Session([_player(user_id=None)]))
        r = await client.get("/player/privacy?player_name=Alice&acc_hash=123")
        assert r.status_code == 409
        assert "Discord" in (await r.get_json())["error"]

    async def test_unknown_account_is_a_404(self, client, monkeypatch):
        # Hash misses, then name + hash misses.
        _wire(monkeypatch, _Session([], []))
        r = await client.get("/player/privacy?player_name=Nobody&acc_hash=1")
        assert r.status_code == 404

    async def test_hash_is_required(self, client, monkeypatch):
        _wire(monkeypatch, _Session())
        r = await client.get("/player/privacy?player_name=Alice")
        assert r.status_code == 400


class TestSave:
    async def test_saves_only_what_was_sent(self, client, monkeypatch):
        user = _user(group_ping=True)
        session = _Session([_player()], [user])
        _wire(monkeypatch, session)
        r = await client.post("/player/privacy", json={
            "player_name": "Alice", "acc_hash": "123", "settings": {"hidden": True, "never_ping": True}})
        assert r.status_code == 200
        assert session.committed
        assert (user.hidden, user.never_ping, user.group_ping, user.global_ping) == (True, True, True, False)
        assert (await r.get_json())["settings"]["hidden"] is True

    @pytest.mark.parametrize("settings", [
        {"admin": True},            # not a privacy switch
        {"hidden": "yes"},          # not a boolean
        {},                         # nothing to save
        ["hidden"],                 # not an object
    ])
    async def test_bad_bodies_write_nothing(self, client, monkeypatch, settings):
        session = _Session()
        _wire(monkeypatch, session)
        r = await client.post("/player/privacy", json={
            "player_name": "Alice", "acc_hash": "123", "settings": settings})
        assert r.status_code == 422
        assert not session.committed

    async def test_unlinked_account_saves_nothing(self, client, monkeypatch):
        session = _Session([_player(user_id=None)])
        _wire(monkeypatch, session)
        r = await client.post("/player/privacy", json={
            "player_name": "Alice", "acc_hash": "123", "settings": {"hidden": True}})
        assert r.status_code == 409
        assert not session.committed


class TestHiddenGlobally:
    @pytest.mark.parametrize("row,expected", [
        ((False, False), False),
        ((True, False), True),    # this account hidden
        ((False, True), True),    # "Hide me everywhere"
        ((None, None), False),    # no linked user
    ])
    def test_global_group_honours_both_flags(self, row, expected):
        assert common.player_hidden_globally(_Session([row]), 2, 3) is expected

    def test_own_groups_are_never_filtered(self):
        # No query at all: a clan's own Discord keeps announcing its member.
        assert common.player_hidden_globally(_Session(), 14, 3) is False

    def test_unknown_player_and_lookup_errors_fail_open(self):
        assert common.player_hidden_globally(_Session([]), 2, 3) is False

        class Broken(_Session):
            def query(self, *a, **k):
                raise RuntimeError("db down")

        assert common.player_hidden_globally(Broken(), 2, 3) is False
