"""Account-type filter on GET /api/v1/leaderboards/players.

``?account_type=`` narrows a player board to one game mode (or a family such
as ``any_ironman``) and re-ranks within it; anything unknown is the unfiltered
board. Every row carries the player's mode when it is known.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from utils.account_types import ACCOUNT_TYPE_FILTERS, account_type_filter_modes


class TestFilterKeys:
    def test_exact_mode_is_its_own_key(self):
        assert account_type_filter_modes("ultimate_ironman") == frozenset({"ultimate_ironman"})

    def test_any_ironman_excludes_normal(self):
        modes = account_type_filter_modes("any_ironman")
        assert "normal" not in modes
        assert {"ironman", "group_ironman", "unranked_group_ironman"} <= modes

    def test_any_group_ironman_is_the_three_group_modes(self):
        assert account_type_filter_modes("any_group_ironman") == frozenset(
            {"group_ironman", "hardcore_group_ironman", "unranked_group_ironman"}
        )

    @pytest.mark.parametrize("key", [None, "", "all", "bogus", "constructor"])
    def test_unknown_is_all_players(self, key):
        assert account_type_filter_modes(key) is None

    def test_case_insensitive(self):
        assert account_type_filter_modes(" IronMan ") == frozenset({"ironman"})

    def test_every_key_covers_something(self):
        assert all(ACCOUNT_TYPE_FILTERS.values())


# Board order: 1 normal, 2 ironman, 3 hidden ironman, 4 uim, 5 unknown, 6 ironman
_BOARD = [(b"1", 600.0), (b"2", 500.0), (b"3", 400.0), (b"4", 300.0), (b"5", 200.0), (b"6", 100.0)]
_TYPES = {1: "normal", 2: "ironman", 3: "ironman", 4: "ultimate_ironman", 6: "ironman"}


class _Conn:
    def __init__(self):
        self.full_reads = 0

    def zrevrange(self, key, start, end, withscores=False):
        if start == 0 and end == -1:
            self.full_reads += 1
            return list(_BOARD)
        return _BOARD[start:end + 1]

    def zcard(self, key):
        return len(_BOARD)


@pytest.fixture()
def conn(monkeypatch):
    import web_api.routes.leaderboards as lb

    c = _Conn()
    session = MagicMock()
    session.query.return_value.filter.return_value.all.side_effect = lambda: [
        (pid, f"P{pid}") for pid in range(1, 7)
    ]

    @contextmanager
    def fake_db_session():
        yield session

    monkeypatch.setattr(lb, "_rc", lambda: c)
    monkeypatch.setattr(lb, "db_session", fake_db_session)
    monkeypatch.setattr(lb, "hidden_player_ids", lambda: {3})
    monkeypatch.setattr(lb, "player_account_types", lambda: dict(_TYPES))
    monkeypatch.setattr(lb, "_compact_badges_for", lambda ids: {})
    monkeypatch.setattr(lb, "_player_groups_for", lambda ids, token: {})
    # The filtered board is memoized per (key, filter); start every test cold.
    store: dict = {}
    monkeypatch.setattr(lb, "cache_get", lambda k, ttl: store.get(k))
    monkeypatch.setattr(lb, "cache_set", lambda k, v: store.__setitem__(k, v))
    return c


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


async def _get(client, qs):
    r = await client.get(f"/api/v1/leaderboards/players?period=all{qs}")
    assert r.status_code == 200
    return await r.get_json()


class TestPlayersRoute:
    async def test_unfiltered_keeps_redis_ranks_and_tags_modes(self, client, conn):
        body = await _get(client, "")
        rows = [(e["rank"], e["id"], e.get("account_type")) for e in body["entries"]]
        assert rows == [
            (1, 1, "normal"),
            (2, 2, "ironman"),
            (4, 4, "ultimate_ironman"),
            (5, 5, None),
            (6, 6, "ironman"),
        ]
        assert body["meta"]["total"] == 6
        assert "account_type" not in body

    async def test_filter_reranks_within_mode_and_drops_hidden(self, client, conn):
        body = await _get(client, "&account_type=ironman")
        assert [(e["rank"], e["id"]) for e in body["entries"]] == [(1, 2), (2, 6)]
        assert body["meta"]["total"] == 2
        assert body["account_type"] == "ironman"

    async def test_family_filter(self, client, conn):
        body = await _get(client, "&account_type=any_ironman")
        assert [e["id"] for e in body["entries"]] == [2, 4, 6]

    async def test_filter_pages(self, client, conn):
        body = await _get(client, "&account_type=any_ironman&page=2&limit=2")
        assert [(e["rank"], e["id"]) for e in body["entries"]] == [(3, 6)]
        assert body["meta"]["total"] == 3

    async def test_filtered_board_is_memoized(self, client, conn):
        await _get(client, "&account_type=ironman")
        await _get(client, "&account_type=ironman&page=2")
        assert conn.full_reads == 1

    async def test_unknown_filter_is_the_full_board(self, client, conn):
        body = await _get(client, "&account_type=nonsense")
        assert len(body["entries"]) == 5
        assert "account_type" not in body
