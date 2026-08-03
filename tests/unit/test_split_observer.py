"""services/split_observer.py — TEMP split-source observation counters,
version gating, guid dedupe and bucketing, against an in-memory fake Redis."""

import importlib
import json

# The stubbed ``services`` parent package would swallow attribute imports;
# import_module resolves the real module conftest registered in sys.modules.
so = importlib.import_module("services.split_observer")


class FakePipeline:
    def __init__(self, store):
        self.store = store
        self.results = []

    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.results.append(getattr(self.store, name)(*args, **kwargs))
            return self

        return _op

    def execute(self):
        out, self.results = self.results, []
        return out


class FakeRedis:
    """Just enough of redis-py for split_observer."""

    def __init__(self):
        self.kv = {}
        self.hashes = {}
        self.sets = {}
        self.lists = {}

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    def get(self, key):
        v = self.kv.get(key)
        return str(v).encode() if v is not None else None

    def expire(self, key, ttl):
        return True

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    def smembers(self, key):
        return {str(m).encode() for m in self.sets.get(key, set())}

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or {})
        return len(mapping or {})

    def hincrby(self, key, field, amount=1):
        h = self.hashes.setdefault(key, {})
        h[field] = int(h.get(field, 0)) + amount
        return h[field]

    def hgetall(self, key):
        return {
            str(k).encode(): str(v).encode()
            for k, v in self.hashes.get(key, {}).items()
        }

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def ltrim(self, key, start, stop):
        self.lists[key] = self.lists.get(key, [])[start:stop + 1]
        return True

    def lrange(self, key, start, stop):
        return [str(v).encode() for v in self.lists.get(key, [])[start:stop + 1]]


def _stats(r, npc_id=13668):
    snap = so.snapshot(r=r)
    return snap["npcs"][npc_id]


def test_capable_version_gating():
    r = FakeRedis()
    so.record_drop(npc_id=13668, npc_name="Theatre of Blood", player_name="A",
                   players=["B", "C"], plugin_version="5.4.0", guid="g1", r=r)
    so.record_drop(npc_id=13668, npc_name="Theatre of Blood", player_name="A",
                   players=None, plugin_version="5.2.5", guid="g2", r=r)
    so.record_drop(npc_id=13668, npc_name="Theatre of Blood", player_name="A",
                   players=None, plugin_version=None, guid="g3", r=r)
    stats = _stats(r)
    assert stats["kills"] == 3
    assert stats["capable"] == 1
    assert stats["with_players"] == 1
    assert stats["players_sum"] == 2
    assert stats["n2"] == 1


def test_multi_embed_same_guid_counts_one_kill():
    r = FakeRedis()
    for _ in range(3):
        so.record_drop(npc_id=1, npc_name="Chambers of Xeric", player_name="A",
                       players=["B"], plugin_version="5.4.0", guid="same", r=r)
    stats = _stats(r, 1)
    assert stats["drops"] == 3
    assert stats["kills"] == 1
    assert stats["with_players"] == 1


def test_sample_recorded_and_capped_fields():
    r = FakeRedis()
    so.record_drop(npc_id=2, npc_name="Nex", player_name="Me",
                   players=["P1", "P2", "P3"], plugin_version="5.3.0", guid="g", r=r)
    samples = so.get_samples(2, r=r)
    assert len(samples) == 1
    assert samples[0]["p"] == "Me"
    assert samples[0]["n"] == ["P1", "P2", "P3"]
    raw = json.loads(r.lists["splitscan:samples:2"][0])
    assert raw["k"] == "drop"


def test_record_kill_time_roster_and_team_size():
    r = FakeRedis()
    so.record_kill_time(npc_id=3, npc_name="Tombs of Amascut", player_name="A",
                        raw_nearby="B, C,D", team_size="4", plugin_version="5.4.0",
                        guid="kt1", r=r)
    so.record_kill_time(npc_id=3, npc_name="Tombs of Amascut", player_name="A",
                        raw_nearby="none", team_size="Solo", plugin_version="5.4.0",
                        guid="kt2", r=r)
    stats = _stats(r, 3)
    assert stats["kt_kills"] == 2
    assert stats["kt_capable"] == 2
    assert stats["kt_with_players"] == 1
    assert stats["kt_players_sum"] == 3
    assert stats["kt_team_reported"] == 2
    assert stats["kt_team_gt1"] == 1
    assert stats["kt_team_sum"] == 5


def test_parse_nearby_shapes():
    assert so.parse_nearby("A,B") == ["A", "B"]
    assert so.parse_nearby('["A","B"]') == ["A", "B"]
    assert so.parse_nearby(["A", " B "]) == ["A", "B"]
    assert so.parse_nearby("none") == []
    assert so.parse_nearby("None") == []
    assert so.parse_nearby("") == []
    assert so.parse_nearby(None) == []
    assert so.parse_nearby(7) == []


def test_pv_parsing():
    assert so.pv_capable("5.3.0")
    assert so.pv_capable("5.4.1")
    assert so.pv_capable("10.0")
    assert not so.pv_capable("5.2.5")
    assert not so.pv_capable("4.9.9")
    assert not so.pv_capable(None)
    assert not so.pv_capable("beta")
    assert so.pv_tuple("5.4.0-rc1") == (5, 4, 0)


def test_team_size_count():
    assert so.team_size_count("Solo") == 1
    assert so.team_size_count("2") == 2
    assert so.team_size_count("11-15") == 11
    assert so.team_size_count("6+") == 6
    assert so.team_size_count("") is None
    assert so.team_size_count("weird") is None


def test_classify_buckets():
    raid = {"name": "Theatre of Blood: Hard Mode", "capable": 2, "with_players": 2}
    assert so.classify(raid)[0] == so.BUCKET_RAID

    low = {"name": "Zulrah", "capable": 5, "with_players": 0}
    assert so.classify(low)[0] == so.BUCKET_LOW_DATA

    solo = {"name": "Vorkath", "capable": 200, "with_players": 1}
    assert so.classify(solo)[0] == so.BUCKET_SOLO

    accompanied = {"name": "General Graardor", "capable": 100, "with_players": 40,
                   "kt_capable": 20, "kt_with_players": 10}
    bucket, rate = so.classify(accompanied)
    assert bucket == so.BUCKET_ACCOMPANIED
    assert abs(rate - 50 / 120) < 1e-9

    mixed = {"name": "Corporeal Beast", "capable": 100, "with_players": 5}
    assert so.classify(mixed)[0] == so.BUCKET_MIXED


def test_fail_open_without_redis():
    # A connection-less object (no .pipeline, no .client) resolves to conn=None;
    # r=None would fall back to the real singleton, which in prod is live Redis.
    dead = object()
    so.record_drop(npc_id=1, npc_name="X", player_name="A", players=["B"],
                   plugin_version="5.4.0", guid="g", r=dead)
    so.record_kill_time(npc_id=1, npc_name="X", player_name="A", raw_nearby="B",
                        team_size="2", plugin_version="5.4.0", guid="g2", r=dead)
    assert so.snapshot(r=dead) == {"started": None, "npcs": {}}
    assert so.get_samples(1, r=dead) == []
