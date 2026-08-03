"""services/status_metrics.py — window sums, active players, heartbeats,
and the derived service snapshot, against an in-memory fake Redis."""

import importlib

import pytest

# The stubbed ``services`` parent package would swallow attribute imports;
# import_module resolves the real module conftest registered in sys.modules.
sm = importlib.import_module("services.status_metrics")


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
    """Just enough of redis-py for status_metrics."""

    def __init__(self):
        self.kv = {}
        self.zsets = {}

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def incrby(self, key, amount=1):
        self.kv[key] = int(self.kv.get(key, 0)) + amount
        return self.kv[key]

    def incr(self, key):
        return self.incrby(key, 1)

    def expire(self, key, ttl):
        return True

    def setex(self, key, ttl, value):
        self.kv[key] = value
        return True

    def get(self, key):
        v = self.kv.get(key)
        return str(v).encode() if v is not None else None

    def mget(self, keys):
        return [self.get(k) for k in keys]

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zremrangebyscore(self, key, lo, hi):
        z = self.zsets.get(key, {})
        hi = float(hi)
        removed = [m for m, score in z.items() if score <= hi]
        for m in removed:
            del z[m]
        return len(removed)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def llen(self, key):
        return int(self.kv.get(f"__len__{key}", 0))


NOW = 1_800_000_000.0


@pytest.fixture
def r():
    return FakeRedis()


def test_conn_accepts_raw_redis_with_client_method(r):
    # redis.Redis has a *method* named ``client`` — the raw connection itself
    # must be used, not that bound method (regression: silent heartbeat no-op).
    r.client = lambda: "not-a-connection"
    sm.heartbeat("webhook_consumer", r=r, now=NOW - 5)
    assert sm.get_heartbeat_age("webhook_consumer", r=r, now=NOW) == 5


def test_conn_unwraps_redisclient_wrapper(r):
    class Wrapper:  # shape of utils.redis.RedisClient
        def __init__(self, client):
            self.client = client

    sm.heartbeat("webhook_bot", r=Wrapper(r), now=NOW - 7)
    assert sm.get_heartbeat_age("webhook_bot", r=Wrapper(r), now=NOW) == 7


def test_player_key_prefers_first_usable():
    assert sm.player_key(None, "  Player One  ", "hash") == "player one"
    assert sm.player_key("", None, 12345) == "12345"
    assert sm.player_key(None, "") is None


def test_processed_counts_window_boundaries(r):
    sm.record_processed(sm.SOURCE_API, "alice", r=r, now=NOW)
    sm.record_processed(sm.SOURCE_API, "bob", r=r, now=NOW - 4 * 60)      # in 5m
    sm.record_processed(sm.SOURCE_API, "carol", r=r, now=NOW - 29 * 60)   # in 30m
    sm.record_processed(sm.SOURCE_API, "dave", r=r, now=NOW - 23 * 3600)  # in 24h
    sm.record_processed(sm.SOURCE_API, "old", r=r, now=NOW - 25 * 3600)   # outside

    counts = sm.get_processed_counts(sm.SOURCE_API, r=r, now=NOW)
    assert counts == {"5m": 2, "30m": 3, "24h": 4}


def test_counts_are_per_source(r):
    sm.record_processed(sm.SOURCE_API, "alice", r=r, now=NOW)
    sm.record_processed(sm.SOURCE_WEBHOOK, "bob", r=r, now=NOW)
    sm.record_processed(sm.SOURCE_WEBHOOK, "carol", r=r, now=NOW)

    assert sm.get_processed_counts(sm.SOURCE_API, r=r, now=NOW)["24h"] == 1
    assert sm.get_processed_counts(sm.SOURCE_WEBHOOK, r=r, now=NOW)["24h"] == 2


def test_active_players_trims_stale(r):
    sm.record_processed(sm.SOURCE_API, "fresh", r=r, now=NOW - 300)
    sm.record_processed(sm.SOURCE_API, "edge", r=r, now=NOW - 3500)
    sm.record_processed(sm.SOURCE_API, "stale", r=r, now=NOW - 7200)
    # Same player twice counts once (zset member updated in place).
    sm.record_processed(sm.SOURCE_API, "fresh", r=r, now=NOW - 60)

    assert sm.get_active_players(sm.SOURCE_API, r=r, now=NOW) == 2
    # Stale member physically removed by the read-side trim.
    assert "stale" not in r.zsets[sm._players_key(sm.SOURCE_API)]


def test_heartbeat_age(r):
    assert sm.get_heartbeat_age("webhook_consumer", r=r, now=NOW) is None
    sm.heartbeat("webhook_consumer", r=r, now=NOW - 42)
    assert sm.get_heartbeat_age("webhook_consumer", r=r, now=NOW) == 42


def test_issues_rev_roundtrip(r):
    assert sm.get_issues_rev(r=r) == 0
    sm.bump_issues_rev(r=r)
    sm.bump_issues_rev(r=r)
    assert sm.get_issues_rev(r=r) == 2


def test_record_processed_fail_open():
    class Boom:
        def pipeline(self, transaction=True):
            raise RuntimeError("redis down")

    # Must not raise — writers sit on the intake path.
    sm.record_processed(sm.SOURCE_API, "alice", r=Boom(), now=NOW)


def _snapshot(monkeypatch, r, *, ping, consumer_beat, webhook_beat, depth=0):
    monkeypatch.setattr(sm, "ping_api", lambda timeout=2.0: ping)
    if consumer_beat:
        sm.heartbeat(sm.HEARTBEAT_CONSUMER, r=r, now=NOW - 10)
    if webhook_beat:
        sm.heartbeat(sm.HEARTBEAT_WEBHOOK_BOT, r=r, now=NOW - 10)
    r.kv["__len__webhook:queue"] = depth
    return sm.collect_service_snapshot(r=r, now=NOW)


def test_snapshot_all_operational(monkeypatch, r):
    snap = _snapshot(monkeypatch, r, ping=True, consumer_beat=True, webhook_beat=True)
    assert snap["api"]["status"] == "operational"
    assert snap["webhook"]["status"] == "operational"
    assert snap["api"]["consumer_alive"] is True


def test_snapshot_api_offline(monkeypatch, r):
    snap = _snapshot(monkeypatch, r, ping=False, consumer_beat=True, webhook_beat=True)
    assert snap["api"]["status"] == "offline"
    assert snap["api"]["online"] is False


def test_snapshot_degraded_on_stale_consumer(monkeypatch, r):
    snap = _snapshot(monkeypatch, r, ping=True, consumer_beat=False, webhook_beat=True)
    assert snap["api"]["status"] == "degraded"
    assert snap["api"]["consumer_alive"] is False


def test_snapshot_degraded_on_backlog(monkeypatch, r):
    snap = _snapshot(monkeypatch, r, ping=True, consumer_beat=True, webhook_beat=True,
                     depth=sm.QUEUE_BACKLOG_DEGRADED)
    assert snap["api"]["status"] == "degraded"
    assert snap["api"]["queue_depth"] == sm.QUEUE_BACKLOG_DEGRADED


def test_snapshot_webhook_offline(monkeypatch, r):
    snap = _snapshot(monkeypatch, r, ping=True, consumer_beat=True, webhook_beat=False)
    assert snap["webhook"]["status"] == "offline"
    assert snap["webhook"]["online"] is False
