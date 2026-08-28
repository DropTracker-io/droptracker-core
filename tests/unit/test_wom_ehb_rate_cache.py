"""The shared EHB rate cache must renew itself, not hide behind a memo.

EHE prices every effort row as ``kills / rate``; the rates come from one Redis
key (``wom:ehb_rates:main``) that ONLY the events worker refreshes and that the
web API reads with no fallback of its own. On 2026-08-28 that key had been
expired for a day and every WOM-priced boss on every event surface scored 0
hours — a player with 249 Sarachnis, 233 Zulrah and 131 Phosani's read "~1.4h",
which was just the leftovers priced from the ``npc_ehb_rates`` DB fallback.

The key had expired because ``get_ehb_rates_sync`` falls back to this process's
memo when Redis misses (deliberate: a stale rate table beats none), and the
refresher decided "already cached, nothing to do" from that same call. The
worker served its own memo indefinitely, never re-fetched, and so never rewrote
the key that everyone else depends on. Freshness must come from the SHARED
cache's TTL, never from a value the local memo can supply.

Like test_wom_degenerate_identity.py, the conftest stubs ``utils.wiseoldman``
with a MagicMock, so the real module is loaded by file path.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

RATES_JSON = b'[{"boss":"zulrah","rate":46},{"boss":"sarachnis","rate":110}]'


class _FakeRedis:
    """Enough of the redis client for the rate-cache paths, with a TTL the test
    sets directly — ``-2`` is redis's "no such key", ``-1`` "no expiry"."""

    def __init__(self, value=None, ttl=-2):
        self.value = value
        self._ttl = ttl
        self.setex_calls = []
        self.raise_on_ttl = False

    def get(self, _key):
        return self.value

    def ttl(self, _key):
        if self.raise_on_ttl:
            raise RuntimeError("redis down")
        return self._ttl

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.value = value
        self._ttl = ttl


class _RedisHolder:
    def __init__(self, client):
        self.client = client


class _Limiter:
    def __init__(self, allow=True):
        self.allow = allow
        self.waits = 0

    async def wait(self):
        self.waits += 1
        return self.allow


class _Http:
    def __init__(self, payload):
        self.payload = payload
        self.fetches = 0

    async def fetch(self, _route):
        self.fetches += 1
        return self.payload


class _Client:
    def __init__(self, payload=RATES_JSON):
        self._http = _Http(payload)

    async def start(self):
        return None


@pytest.fixture
def wom():
    """The real module over the conftest stubs, restored afterwards."""
    db_mod = types.ModuleType("db")
    models_ns = types.ModuleType("db.models")
    models_ns.Player = type("Player", (), {})
    models_ns.Group = type("Group", (), {})
    models_ns.NpcList = type("NpcList", (), {})
    db_mod.models = models_ns
    db_mod.NpcList = models_ns.NpcList
    db_mod.Player = models_ns.Player
    db_mod.session = None

    utils_redis_stub = types.ModuleType("utils.redis")

    class _NoopRedis:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    utils_redis_stub.redis_client = _NoopRedis()

    services_ru_stub = types.ModuleType("services.redis_updates")
    services_ru_stub.get_player_list_loot_sum = lambda ids: 0

    saved = {}

    def swap(name, module):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module

    swap("db", db_mod)
    swap("db.models", models_ns)
    swap("utils.redis", utils_redis_stub)
    swap("services.redis_updates", services_ru_stub)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    try:
        spec = importlib.util.spec_from_file_location(
            "_real_wiseoldman_ehb_rates", REPO_ROOT / "utils/wiseoldman.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_real_wiseoldman_ehb_rates"] = mod
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_real_wiseoldman_ehb_rates", None)


def _wire(mod, *, redis, limiter_allows=True, payload=RATES_JSON):
    mod.redis_client = _RedisHolder(redis)
    mod.limiter = _Limiter(limiter_allows)
    mod.client = _Client(payload)
    mod._ehb_rates_memo.clear()
    mod._ehb_rates_last_attempt.clear()
    return mod.client._http


class TestNeedsRefresh:
    def test_absent_key_needs_refresh(self, wom):
        _wire(wom, redis=_FakeRedis(ttl=-2))
        assert wom._ehb_rates_needs_refresh("main") is True

    def test_healthy_ttl_does_not(self, wom):
        _wire(wom, redis=_FakeRedis(ttl=wom.EHB_RATES_REFRESH_BEFORE + 60))
        assert wom._ehb_rates_needs_refresh("main") is False

    def test_ttl_inside_the_renewal_window_does(self, wom):
        # The whole point: renew BEFORE the key lapses, because the moment it
        # does, every other process reads an empty map.
        _wire(wom, redis=_FakeRedis(ttl=wom.EHB_RATES_REFRESH_BEFORE - 60))
        assert wom._ehb_rates_needs_refresh("main") is True

    def test_persistent_key_is_left_alone(self, wom):
        # -1 = present, no expiry (someone primed it by hand during an outage).
        _wire(wom, redis=_FakeRedis(ttl=-1))
        assert wom._ehb_rates_needs_refresh("main") is False

    def test_unreachable_redis_is_not_a_refresh_signal(self, wom):
        redis = _FakeRedis(ttl=-2)
        redis.raise_on_ttl = True
        _wire(wom, redis=redis)
        # None, not True: a fetch we cannot cache helps nobody else.
        assert wom._ehb_rates_needs_refresh("main") is None


class TestGetEhbRatesRenewal:
    def test_stale_memo_does_not_suppress_the_refetch(self, wom):
        """The 2026-08-28 regression, exactly.

        Memo warm from an earlier fetch, Redis key expired: the old code
        returned the memo and never called WOM, so the key everyone else reads
        stayed gone.
        """
        redis = _FakeRedis(value=None, ttl=-2)
        http = _wire(wom, redis=redis)
        wom._ehb_rates_memo["main"] = (0.0, {"zulrah": 46.0})

        rates = asyncio.run(wom.get_ehb_rates())

        assert http.fetches == 1
        assert rates == {"zulrah": 46.0, "sarachnis": 110.0}
        assert len(redis.setex_calls) == 1
        key, ttl, value = redis.setex_calls[0]
        assert key == "wom:ehb_rates:main"
        assert ttl == wom.EHB_RATES_CACHE_TTL
        assert json.loads(value)["sarachnis"] == 110

    def test_healthy_cache_costs_no_wom_call(self, wom):
        redis = _FakeRedis(value=json.dumps({"zulrah": 46}),
                           ttl=wom.EHB_RATES_REFRESH_BEFORE + 3600)
        http = _wire(wom, redis=redis)

        assert asyncio.run(wom.get_ehb_rates()) == {"zulrah": 46.0}
        assert http.fetches == 0

    def test_failed_fetch_does_not_become_a_poll(self, wom):
        """A cold cache plus an unhappy WOM must back off, not hammer.

        The caller is the events worker's 30s state-refresh loop.
        """
        redis = _FakeRedis(value=None, ttl=-2)
        http = _wire(wom, redis=redis, limiter_allows=False)

        for _ in range(5):
            assert asyncio.run(wom.get_ehb_rates()) == {}
        assert http.fetches == 0
        # One limiter attempt, then the retry floor holds the rest off.
        assert wom.limiter.waits == 1

    def test_unreachable_redis_serves_the_memo_without_fetching(self, wom):
        redis = _FakeRedis(ttl=-2)
        redis.raise_on_ttl = True
        http = _wire(wom, redis=redis)
        wom._ehb_rates_memo["main"] = (0.0, {"zulrah": 46.0})

        assert asyncio.run(wom.get_ehb_rates()) == {"zulrah": 46.0}
        assert http.fetches == 0


class TestGetEhbRatesSync:
    def test_redis_miss_still_serves_the_memo_to_readers(self, wom):
        """Readers keep the stale fallback — a rate table that moves on Jagex
        rebalances is far better stale than absent. Only the *refresher* is
        forbidden from trusting it."""
        _wire(wom, redis=_FakeRedis(value=None, ttl=-2))
        wom._ehb_rates_memo["main"] = (0.0, {"zulrah": 46.0})
        assert wom.get_ehb_rates_sync() == {"zulrah": 46.0}

    def test_no_memo_and_no_key_is_empty(self, wom):
        _wire(wom, redis=_FakeRedis(value=None, ttl=-2))
        assert wom.get_ehb_rates_sync() == {}
