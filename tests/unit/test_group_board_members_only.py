"""Per-group player boards show current members only (bug #235).

The ``leaderboard:{token}:group:{gid}`` sorted sets are only ever added to at
drop time, so a player removed from the group (WOM sync, claim, admin) stays
on them for the rest of the period. The group profile's "Top players this
month" and the group-scoped leaderboard filter them out at read time.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

# 1 left the group, 2 member, 3 hidden member, 4 member, 5 left
_BOARD = [(b"1", 900.0), (b"2", 800.0), (b"3", 700.0), (b"4", 600.0), (b"5", 500.0)]
_MEMBERS = {2, 3, 4}


class _Conn:
    def zrevrange(self, key, start, end, withscores=False):
        return list(_BOARD) if end == -1 else _BOARD[start:end + 1]

    def zcard(self, key):
        return len(_BOARD)


class TestGroupMemberIds:
    def test_global_group_is_never_filtered(self):
        from web_api.common import group_member_ids

        assert group_member_ids(2) is None
        assert group_member_ids("not an id") is None

    def test_a_db_fault_fails_open(self, monkeypatch):
        import web_api.common as common

        @contextmanager
        def broken():
            raise RuntimeError("db down")
            yield

        monkeypatch.setattr(common, "cache_get", lambda *a: None)
        monkeypatch.setattr(common, "db_session", broken)
        assert common.group_member_ids(225) is None


class TestProfileTopPlayers:
    def _top(self, monkeypatch, members):
        import web_api.routes.profiles as profiles

        monkeypatch.setattr(profiles, "group_member_ids", lambda gid: members)
        session = MagicMock()
        session.query.return_value.filter.return_value.all.return_value = [
            MagicMock(player_id=pid, player_name=f"P{pid}") for pid in range(1, 6)
        ]
        return profiles._group_top_players(session, _Conn(), 225, 202611, {3})

    def test_players_who_left_are_not_listed(self, monkeypatch):
        out = self._top(monkeypatch, set(_MEMBERS))
        assert [(r["rank"], r["id"]) for r in out] == [(1, 2), (2, 4)]

    def test_unknown_membership_keeps_the_board(self, monkeypatch):
        out = self._top(monkeypatch, None)
        assert [r["id"] for r in out] == [1, 2, 4, 5]


@pytest.fixture()
def client(monkeypatch):
    import web_api
    import web_api.routes.leaderboards as lb

    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = [
        (pid, f"P{pid}") for pid in range(1, 6)
    ]

    @contextmanager
    def fake_db_session():
        yield session

    monkeypatch.setattr(lb, "_rc", lambda: _Conn())
    monkeypatch.setattr(lb, "db_session", fake_db_session)
    monkeypatch.setattr(lb, "hidden_player_ids", lambda: {3})
    monkeypatch.setattr(lb, "player_account_types", lambda: {})
    monkeypatch.setattr(lb, "group_member_ids", lambda gid: set(_MEMBERS) if gid == 225 else None)
    monkeypatch.setattr(lb, "_compact_badges_for", lambda ids: {})
    monkeypatch.setattr(lb, "_player_groups_for", lambda ids, token: {})
    store: dict = {}
    monkeypatch.setattr(lb, "cache_get", lambda k, ttl: store.get(k))
    monkeypatch.setattr(lb, "cache_set", lambda k, v: store.__setitem__(k, v))
    return web_api.create_app().test_client()


async def _get(client, scope):
    r = await client.get(f"/api/v1/leaderboards/players?period=all&scope={scope}")
    assert r.status_code == 200
    return await r.get_json()


class TestGroupScopedLeaderboard:
    async def test_group_board_ranks_current_members_only(self, client):
        body = await _get(client, "group:225")
        assert [(e["rank"], e["id"]) for e in body["entries"]] == [(1, 2), (2, 4)]
        assert body["meta"]["total"] == 2

    async def test_global_board_is_untouched(self, client):
        body = await _get(client, "global")
        assert [e["id"] for e in body["entries"]] == [1, 2, 4, 5]
        assert body["meta"]["total"] == 5
