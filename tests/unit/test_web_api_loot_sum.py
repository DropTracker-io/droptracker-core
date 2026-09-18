"""web_api.common.player_list_loot_sum — a roster's loot total in a handful of
round trips instead of one (or two) per member.

The sum feeds every group profile and the cross-group totals behind group
ranks. It was the reason GET /groups/2 took seconds: ~28k members, ~56k
sequential reads.
"""

import pytest

import web_api.common as common

PART = 202609


class FakeRedis:
    def __init__(self, totals=None, board=None):
        self.totals = totals or {}      # player:{id}:{part}:total_loot
        self.board = board or {}        # leaderboard:{part}
        self.calls = []

    def mget(self, keys):
        self.calls.append(("mget", len(keys)))
        return [self.totals.get(k) for k in keys]

    def zmscore(self, key, members):
        self.calls.append(("zmscore", key, len(members)))
        return [self.board.get(m) for m in members]


def _key(pid, part=PART):
    return f"player:{pid}:{part}:total_loot"


@pytest.fixture
def use(monkeypatch):
    def _use(fake):
        monkeypatch.setattr(common, "_rc", lambda: fake)
        return fake

    return _use


def test_total_key_else_global_board_else_nothing(use):
    use(FakeRedis(
        totals={_key(1): b"1500000", _key(2): b"250000.0"},
        board={3: 40_000.0},               # no total key: falls back to the board
    ))                                     # player 4 is on neither

    assert common.player_list_loot_sum([1, 2, 3, 4], PART) == 1_790_000


def test_round_trips_scale_with_batches_not_members(use):
    ids = list(range(1, 2501))
    fake = use(FakeRedis(totals={_key(pid): b"10" for pid in ids[:5]}))

    assert common.player_list_loot_sum(ids, PART) == 50
    # 2,500 members: three MGETs and three ZMSCOREs. It used to be ~5,000 calls.
    assert [c[0] for c in fake.calls].count("mget") == 3
    assert [c[0] for c in fake.calls].count("zmscore") == 3


def test_a_batch_with_every_key_present_never_asks_the_board(use):
    fake = use(FakeRedis(totals={_key(1): b"5", _key(2): b"7"}))

    assert common.player_list_loot_sum([1, 2], PART) == 12
    assert [c[0] for c in fake.calls] == ["mget"]


def test_period_tokens_read_that_periods_board(use):
    # Leaderboards pass week/day/all tokens, which have no per-player total
    # key at all: everything comes from the token's own board.
    fake = use(FakeRedis(board={1: 900.0, 2: 100.0}))

    assert common.player_list_loot_sum([1, 2], "2026W38") == 1000
    assert fake.calls[-1][1] == "leaderboard:2026W38"


def test_an_unreadable_value_counts_as_zero_without_losing_the_rest(use):
    use(FakeRedis(totals={_key(1): b"not-a-number", _key(2): b"300"}))

    assert common.player_list_loot_sum([1, 2], PART) == 300


def test_empty_roster_and_none_ids(use):
    fake = use(FakeRedis(totals={_key(7): b"70"}))

    assert common.player_list_loot_sum([], PART) == 0
    assert fake.calls == []
    assert common.player_list_loot_sum([None, 7, None], PART) == 70


def test_redis_unavailable_reads_as_zero(monkeypatch):
    monkeypatch.setattr(common, "_rc", lambda: None)

    assert common.player_list_loot_sum([1, 2, 3], PART) == 0


def test_a_failing_batch_keeps_what_was_already_read(use):
    class FlakyRedis(FakeRedis):
        def mget(self, keys):
            if self.calls:
                raise ConnectionError("redis went away")
            return super().mget(keys)

    ids = list(range(1, 1501))
    use(FlakyRedis(totals={_key(pid): b"1" for pid in ids}))

    # First thousand read, second batch fails: a partial total, not an exception
    # taking the group page down with it.
    assert common.player_list_loot_sum(ids, PART) == 1000
