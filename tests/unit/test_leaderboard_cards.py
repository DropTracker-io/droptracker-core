"""Card extras on the global leaderboards (the /leaderboards2 card layout).

``_group_card_details`` adds icon, custom description, roster size, members
with loot this period and the period's top earner to each group row;
``_player_groups_for`` adds a player's clans. The ORM is stubbed here, so the
fake session hands back rows in the order the helper queries them; what is
under test is how those rows and the Redis boards are folded into the payload.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

import web_api.routes.leaderboards as lb


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, *results):
        self._results = list(results)

    def query(self, *_a, **_k):
        return _Query(self._results.pop(0))


class _Pipe:
    def __init__(self, boards):
        self._boards = boards
        self._ops = []

    def zcard(self, key):
        self._ops.append(len(self._boards.get(key, [])))

    def zrevrange(self, key, start, end, withscores=False):
        self._ops.append(self._boards.get(key, [])[start:end + 1])

    def execute(self):
        return self._ops


class _Conn:
    def __init__(self, boards):
        self._boards = boards

    def pipeline(self, transaction=True):
        return _Pipe(self._boards)


@pytest.fixture()
def wire(monkeypatch):
    def _wire(session, boards=None, hidden=(), precomputed=None):
        @contextmanager
        def _db():
            yield session

        monkeypatch.setattr(lb, "db_session", _db)
        monkeypatch.setattr(lb, "cache_get", lambda *a, **k: None)
        monkeypatch.setattr(lb, "cache_set", lambda *a, **k: None)
        monkeypatch.setattr(lb, "hidden_player_ids", lambda: set(hidden))
        monkeypatch.setattr(lb, "_rc", lambda: _Conn(boards or {}))
        monkeypatch.setattr(lb, "_read_group_totals_precomputed", lambda _t: precomputed)

    return _wire


def test_group_cards_fold_icon_members_and_top_earner(wire):
    boards = {
        "leaderboard:202609:group:5": [(b"11", 900.0), (b"12", 400.0)],
        "leaderboard:202609:group:6": [],
    }
    session = _Session(
        [(5, "https://x/icon.png", "PvM clan"), (6, None, "An Old School RuneScape group.")],
        [(5, 40), (6, 3)],
        [(11, "Zezima")],
    )
    wire(session, boards)

    out = lb._group_card_details([5, 6], 202609)

    assert out[5] == {
        "active_count": 2,
        "icon_url": "https://x/icon.png",
        "description": "PvM clan",
        "member_count": 40,
        "top_player": {"id": 11, "name": "Zezima", "loot": lb.money(900)},
    }
    # Default description and missing icon are left off; empty board = 0 active.
    assert out[6] == {"active_count": 0, "member_count": 3}


def test_group_top_earner_skips_hidden_players(wire):
    boards = {"leaderboard:all:group:5": [(b"11", 900.0), (b"12", 400.0)]}
    session = _Session([(5, None, None)], [(5, 2)], [(12, "Lynx Titan")])
    wire(session, boards, hidden={11})

    out = lb._group_card_details([5], "all")

    assert out[5]["top_player"]["id"] == 12
    assert out[5]["active_count"] == 2


def test_player_groups_order_by_clan_rank_and_cap(wire):
    session = _Session([
        (1, 30, "Small"),
        (1, 10, "Big"),
        (1, 20, "Mid"),
        (1, 40, "Unranked"),
        (1, 2, "DropTracker"),
        (2, 20, "Mid"),
    ])
    wire(session, precomputed=[(10, 5000), (20, 3000), (30, 100)])

    out = lb._player_groups_for([1, 2, 3], 202609)

    assert [g["id"] for g in out[1]] == [10, 20, 30]
    assert out[2] == [{"id": 20, "name": "Mid"}]
    assert 3 not in out
