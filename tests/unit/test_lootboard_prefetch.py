"""The lootboard's per-player Redis reads are pipelined without changing output.

get_drops_for_group used to issue three single Redis commands per player in
sequence (~81k for the global board). It now prefetches each chunk's keys in
one pipelined round trip. What is pinned: the board data is identical to the
one-command-at-a-time path, and the round trips drop to one per chunk.
"""
from __future__ import annotations

import asyncio
import json

import lootboard.generator as gen

PARTITION = "202610"


class _FakeRedis:
    """Dict-backed stand-in for the sync redis client the generator uses."""

    def __init__(self, hashes, strings, lists):
        self.hashes = hashes
        self.strings = strings
        self.lists = lists
        self.single_calls = 0
        self.pipelines = 0

    def hgetall(self, key):
        self.single_calls += 1
        return dict(self.hashes.get(key, {}))

    def get(self, key):
        self.single_calls += 1
        return self.strings.get(key)

    def lrange(self, key, start, end):
        self.single_calls += 1
        return list(self.lists.get(key, []))

    def pipeline(self, transaction=True):
        self.pipelines += 1
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.queued = []

    def hgetall(self, key):
        self.queued.append(("hgetall", key))

    def get(self, key):
        self.queued.append(("get", key))

    def lrange(self, key, start, end):
        self.queued.append(("lrange", key))

    def execute(self):
        r = self.redis
        out = []
        for op, key in self.queued:
            if op == "hgetall":
                out.append(dict(r.hashes.get(key, {})))
            elif op == "get":
                out.append(r.strings.get(key))
            else:
                out.append(list(r.lists.get(key, [])))
        return out


def _redis_for(player_ids):
    hashes, strings, lists = {}, {}, {}
    for pid in player_ids:
        base = f"player:{pid}:{PARTITION}"
        hashes[f"{base}:total_items"] = {
            b"4151": f"{pid},{pid * 1000},1,0,0".encode(),
            b"995": f"{pid * 10},{pid * 10},2,0,0".encode(),
        }
        if pid % 3:
            strings[f"{base}:total_loot"] = str(pid * 1010).encode()
        lists[f"{base}:recent_items"] = [
            json.dumps({"date_added": f"2026-10-0{pid % 9 + 1} 12:00", "drop_id": pid}).encode()
        ]
    return _FakeRedis(hashes, strings, lists)


class _Client:
    def __init__(self, client):
        self.client = client


_REAL_PREFETCH = gen._prefetch_monthly_player_keys


def _run(monkeypatch, player_ids, *, pipelined, **kwargs):
    fake = _redis_for(player_ids)
    monkeypatch.setattr(gen, "redis_client", _Client(fake))
    monkeypatch.setattr(
        gen, "_prefetch_monthly_player_keys",
        _REAL_PREFETCH if pipelined else (lambda ids, p: {}),
    )
    result = asyncio.run(gen.get_drops_for_group(list(player_ids), PARTITION, **kwargs))
    return result, fake


def test_pipelined_board_matches_single_command_board(monkeypatch):
    player_ids = list(range(1, 1203))  # spans three prefetch chunks
    expected, single = _run(monkeypatch, player_ids, pipelined=False)
    actual, piped = _run(monkeypatch, player_ids, pipelined=True)

    assert actual == expected
    assert single.single_calls == 3 * len(player_ids)
    assert piped.single_calls == 0
    assert piped.pipelines == 3


def test_minimum_value_filter_is_unchanged(monkeypatch):
    player_ids = [1, 2, 600, 601]
    kwargs = {"only_include_items_over_minimum": True, "group_minimum_value": 500}
    expected, _ = _run(monkeypatch, player_ids, pipelined=False, **kwargs)
    actual, _ = _run(monkeypatch, player_ids, pipelined=True, **kwargs)
    assert actual == expected


def test_no_players_makes_no_calls(monkeypatch):
    actual, fake = _run(monkeypatch, [], pipelined=True)
    assert actual[3] == 0
    assert fake.single_calls == 0 and fake.pipelines == 0
