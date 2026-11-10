"""Suggestion #239: post a PB only when it places in the clan's top N."""
from datetime import datetime

import pytest

import utils.pb_rank as pb_rank


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        rows = self.rows

        class _Result:
            def fetchall(self):
                return rows

        return _Result()


def _row(player_id, ms, day=1):
    return ("Solo", player_id, ms, datetime(2026, 11, day), True)


BOARD = [_row(1, 60_000), _row(2, 61_200), _row(3, 62_400), _row(4, 63_000), _row(5, 59_400)]


def test_rank_among_a_subset():
    session = _Session(BOARD)
    assert pb_rank.pb_board_rank(session, 1, "Solo", 3) == (4, 5)
    assert pb_rank.pb_board_rank(session, 1, "Solo", 3, among={1, 2, 3}) == (3, 3)
    assert pb_rank.pb_board_rank(session, 1, "Solo", 3, among={1, 2}) is None


@pytest.fixture
def members(monkeypatch):
    import db.point_standings as standings

    holder = {"ids": set()}
    monkeypatch.setattr(standings, "member_player_ids", lambda _s, _g: set(holder["ids"]))
    return holder


def test_outside_the_clan_top(members):
    members["ids"] = {1, 2, 3}  # player 5 (fastest overall) is not in the clan
    session = _Session(BOARD)
    assert pb_rank.outside_group_top(session, 9, 1, "Solo", 3, 2) is True
    assert pb_rank.outside_group_top(session, 9, 1, "Solo", 2, 2) is False
    assert pb_rank.outside_group_top(session, 9, 1, "Solo", 3, 3) is False


def test_the_player_counts_even_when_not_linked_to_the_group(members):
    members["ids"] = {1}
    assert pb_rank.outside_group_top(_Session(BOARD), 9, 1, "Solo", 2, 1) is True
    assert pb_rank.outside_group_top(_Session(BOARD), 9, 1, "Solo", 2, 2) is False


def test_no_limit_posts_everything(members):
    assert pb_rank.outside_group_top(_Session(BOARD), 9, 1, "Solo", 4, 0) is False


def test_limit_parsing():
    assert pb_rank.top_ranks_limit("5") == 5
    assert pb_rank.top_ranks_limit(None) == 0
    assert pb_rank.top_ranks_limit("0") == 0
    assert pb_rank.top_ranks_limit("-3") == 0
    assert pb_rank.top_ranks_limit("abc") == 0
