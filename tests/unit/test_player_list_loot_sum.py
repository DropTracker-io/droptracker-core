"""get_player_list_loot_sum (services/redis_updates.py) reads in batches.

It used to make one or two Redis round trips per player, synchronously, from
the core bot's event loop. For the global group (~26k members) that was ~47k
round trips on every global drop notification, and those notifications froze
the loop for 7s on median and 24s at worst, which is what dropped the bot's
Discord connections mid-send and led to the duplicate posts of 2026-09-10/12/13.

What must not change is the number it returns: each player's month total key,
else their score on the global board, else nothing.

Loads the real module by path, as test_redis_updates_wrappers.py does, because
conftest stubs ``services.redis_updates``.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

_REAL_PATH = Path(__file__).resolve().parents[2] / "services" / "redis_updates.py"


@pytest.fixture()
def ru():
    spec = importlib.util.spec_from_file_location("_real_redis_updates_loot_sum", _REAL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_real_redis_updates_loot_sum"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("_real_redis_updates_loot_sum", None)


def _partition():
    now = datetime.now()
    return now.year * 100 + now.month


class FakeRedis:
    """Bytes in, like the real client (decode_responses is off)."""

    def __init__(self, totals=None, board=None):
        self.totals = totals or {}
        self.board = board or {}
        self.calls = []

    def mget(self, keys):
        self.calls.append(("mget", len(keys)))
        return [self.totals.get(key) for key in keys]

    def zmscore(self, key, members):
        self.calls.append(("zmscore", len(members)))
        assert key == f"leaderboard:{_partition()}"
        return [self.board.get(member) for member in members]

    def get(self, key):  # the per-player path must not be used
        raise AssertionError("per-player GET")

    def zscore(self, key, member):  # nor this
        raise AssertionError("per-player ZSCORE")


def _install(ru, monkeypatch, fake):
    monkeypatch.setattr(ru, "redis_client", SimpleNamespace(client=fake))


def _key(player_id):
    return f"player:{player_id}:{_partition()}:total_loot"


def test_total_key_else_global_board_else_nothing(ru, monkeypatch):
    fake = FakeRedis(
        totals={_key(1): b"1500000", _key(2): b"250.0"},
        board={3: 7000.0, 1: 999_999_999.0},  # 1 has a key, so its score is not read
    )
    _install(ru, monkeypatch, fake)
    assert ru.get_player_list_loot_sum([1, 2, 3, 4]) == 1_500_000 + 250 + 7000


def test_round_trips_are_per_batch_not_per_player(ru, monkeypatch):
    monkeypatch.setattr(ru, "_LOOT_SUM_BATCH", 2)
    fake = FakeRedis(totals={_key(1): b"1", _key(4): b"4"}, board={2: 2.0, 5: 5.0})
    _install(ru, monkeypatch, fake)
    assert ru.get_player_list_loot_sum([1, 2, 3, 4, 5]) == 12
    assert fake.calls == [
        ("mget", 2), ("zmscore", 1),   # [1, 2]: 2 has no key
        ("mget", 2), ("zmscore", 1),   # [3, 4]: 3 has no key
        ("mget", 1), ("zmscore", 1),   # [5]
    ]


def test_a_batch_with_every_key_present_skips_the_board(ru, monkeypatch):
    fake = FakeRedis(totals={_key(1): b"10", _key(2): b"20"})
    _install(ru, monkeypatch, fake)
    assert ru.get_player_list_loot_sum([1, 2]) == 30
    assert fake.calls == [("mget", 2)]


def test_an_unreadable_value_counts_as_zero_without_losing_the_rest(ru, monkeypatch):
    fake = FakeRedis(totals={_key(1): b"not a number", _key(2): b"40"})
    _install(ru, monkeypatch, fake)
    assert ru.get_player_list_loot_sum([1, 2]) == 40


def test_empty_and_none_ids(ru, monkeypatch):
    fake = FakeRedis(totals={_key(0): b"5"})
    _install(ru, monkeypatch, fake)
    assert ru.get_player_list_loot_sum([]) == 0
    assert fake.calls == []
    # None is skipped; 0 is an id like any other.
    assert ru.get_player_list_loot_sum([None, 0]) == 5


def test_redis_failure_still_reads_as_zero(ru, monkeypatch):
    class Down(FakeRedis):
        def mget(self, keys):
            raise ConnectionError("redis down")

    _install(ru, monkeypatch, Down())
    assert ru.get_player_list_loot_sum([1, 2, 3]) == 0
