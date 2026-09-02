"""Regression tests for the request-volume controls in utils/ge_value.py.

The 2026-08-28 outage had two halves. The visible one was a blocklisted
User-Agent (covered by tests/unit/test_wiki_user_agent.py). The one that
*caused* the blocklist was volume: prices were cached only on success, so
every unpriceable item re-downloaded the ~860KB /mapping document, once per
drop. These tests pin the caching rules that stop that, and the one rule that
must NOT cache — a transport failure, which is what turned a 403 into Araxxor
parts stored at value 0.

Loads the real utils.ge_value in isolation (conftest otherwise stubs it),
with a fake Redis so cache writes are observable.
"""
import importlib.util
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeRedis:
    """Minimal RedisClient stand-in recording setex calls."""

    def __init__(self):
        self.store = {}
        self.setex_calls = []

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, seconds, value):
        self.setex_calls.append((key, seconds, value))
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


def _load_real_ge_value(monkeypatch, redis_client):
    """Import utils/ge_value.py fresh, bypassing the conftest stub.

    The stubs go in via monkeypatch.setitem so they are torn down again — a
    plain ``sys.modules[...] =`` here leaks a Redis-less ``utils.redis`` into
    every later test module in the run.
    """
    monkeypatch.setitem(sys.modules, "aiohttp", MagicMock())

    redis_stub = types.ModuleType("utils.redis")
    redis_stub.RedisClient = lambda *a, **kw: redis_client
    monkeypatch.setitem(sys.modules, "utils.redis", redis_stub)

    vo_stub = types.ModuleType("utils.value_overrides")
    vo_stub.match = lambda item_id, item_name: None
    vo_stub.component_price_key = lambda c: (
        ("id", int(c["item_id"])) if c.get("item_id")
        else ("name", (c.get("item_name") or "").strip().lower())
    )
    monkeypatch.setitem(sys.modules, "utils.value_overrides", vo_stub)

    path = os.path.join(_REPO_ROOT, "utils", "ge_value.py")
    spec = importlib.util.spec_from_file_location("_ge_value_caching_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def redis_client():
    return FakeRedis()


@pytest.fixture()
def ge(monkeypatch, redis_client):
    return _load_real_ge_value(monkeypatch, redis_client)


# --------------------------------------------------------------------------- #
# Price caching
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolved_price_is_cached(ge, redis_client):
    ge._price_by_id = AsyncMock(return_value=1_200_000)
    assert await ge._lookup_and_cache_ge_price("Twisted bow", 20997) == 1_200_000
    assert redis_client.setex_calls == [
        ("item_price:20997", ge.ITEM_PRICE_CACHE_TTL, "1200000")
    ]


@pytest.mark.asyncio
async def test_confirmed_miss_is_cached(ge, redis_client):
    """The API answered and the item has no GE price — cache it.

    This is the path that used to fall through to a full /mapping download on
    every junk drop, forever, because only successes were cached.
    """
    ge._price_by_id = AsyncMock(return_value=None)
    ge._price_by_name = AsyncMock(return_value=None)
    assert await ge._lookup_and_cache_ge_price("Bones", 526) == 0
    assert redis_client.setex_calls == [
        ("item_price:526", ge.ITEM_PRICE_MISS_CACHE_TTL, "0")
    ]


@pytest.mark.asyncio
async def test_transport_failure_is_never_cached(ge, redis_client):
    """A 403/timeout must not be recorded as "this item is worthless".

    Caching it would freeze the outage in for ITEM_PRICE_MISS_CACHE_TTL and
    keep valuing Araxxor parts at 0 long after the API recovered.
    """
    ge._price_by_id = AsyncMock(side_effect=ge.PriceApiUnavailable("HTTP 403"))
    assert await ge._lookup_and_cache_ge_price("Noxious halberd", 29796) == 0
    assert redis_client.setex_calls == []


@pytest.mark.asyncio
async def test_cached_miss_short_circuits_the_api(ge, redis_client):
    redis_client.store["item_price:526"] = "0"
    ge._price_by_id = AsyncMock(return_value=999)
    assert await ge._lookup_and_cache_ge_price("Bones", 526) == 0
    ge._price_by_id.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Override components go through the same cache
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_component_prices_are_deduped_and_cached(ge, redis_client):
    """Two rules sharing a component cost one lookup, and it lands in Redis.

    build_component_price_map used to call the GE API directly, so every
    vestige / Araxxor-part drop re-fetched its component live.
    """
    ge._price_by_id = AsyncMock(return_value=45_000_000)
    overrides = [
        {"components": [{"item_id": 29796, "item_name": "Noxious halberd", "quantity": 1}]},
        {"components": [{"item_id": 29796, "item_name": "Noxious halberd", "quantity": 1}]},
    ]
    price_map = await ge.build_component_price_map(overrides)

    assert price_map == {("id", 29796): 45_000_000}
    assert ge._price_by_id.await_count == 1
    assert redis_client.setex_calls == [
        ("item_price:29796", ge.ITEM_PRICE_CACHE_TTL, "45000000")
    ]


@pytest.mark.asyncio
async def test_unpriced_component_maps_to_none_not_zero(ge):
    """compute_override_from_prices reads falsy as "unpriced" — keep it None."""
    ge._price_by_id = AsyncMock(side_effect=ge.PriceApiUnavailable("HTTP 403"))
    ge._price_by_name = AsyncMock(side_effect=ge.PriceApiUnavailable("HTTP 403"))
    overrides = [{"components": [{"item_id": 29796, "item_name": "Noxious halberd", "quantity": 1}]}]
    assert await ge.build_component_price_map(overrides) == {("id", 29796): None}


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
def test_breaker_opens_after_repeated_failures(ge):
    """At ~670k drops/day, retrying per drop against an endpoint that is
    already refusing us is the behaviour that keeps a block in place."""
    assert not ge._breaker_is_open()
    for _ in range(ge.BREAKER_TRIP_AFTER):
        ge._record_failure()
    assert ge._breaker_is_open()


def test_breaker_closes_on_success(ge):
    for _ in range(ge.BREAKER_TRIP_AFTER):
        ge._record_failure()
    ge._record_success()
    assert not ge._breaker_is_open()


@pytest.mark.asyncio
async def test_open_breaker_skips_the_network(ge):
    for _ in range(ge.BREAKER_TRIP_AFTER):
        ge._record_failure()
    ge.get_prices_session = AsyncMock(side_effect=AssertionError("must not call out"))
    with pytest.raises(ge.PriceApiUnavailable):
        await ge._fetch_latest_price_data(29796)


# --------------------------------------------------------------------------- #
# Mapping cache
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mapping_served_from_redis_without_download(ge, redis_client):
    redis_client.store[ge._MAPPING_REDIS_KEY] = '[{"id": 29796, "name": "Noxious halberd"}]'
    ge._fetch_mapping = AsyncMock(side_effect=AssertionError("must not download"))
    assert await ge.get_mapping() == [{"id": 29796, "name": "Noxious halberd"}]


@pytest.mark.asyncio
async def test_mapping_download_is_cached_for_a_day(ge, redis_client):
    ge._fetch_mapping = AsyncMock(return_value=[{"id": 29796, "name": "Noxious halberd"}])
    await ge.get_mapping()
    assert redis_client.setex_calls[0][0] == ge._MAPPING_REDIS_KEY
    assert redis_client.setex_calls[0][1] == ge.MAPPING_CACHE_TTL


@pytest.mark.asyncio
async def test_repeat_name_lookups_download_the_mapping_once(ge):
    """The 50GB/day question: N name lookups must not be N mapping fetches."""
    ge._fetch_mapping = AsyncMock(return_value=[{"id": 29796, "name": "Noxious halberd"}])
    for _ in range(5):
        assert await ge.find_item_id_by_name("noxious halberd") == 29796
    assert ge._fetch_mapping.await_count == 1


@pytest.mark.asyncio
async def test_unfetchable_mapping_returns_none(ge):
    ge._fetch_mapping = AsyncMock(side_effect=ge.PriceApiUnavailable("HTTP 403"))
    assert await ge.get_mapping() is None
    assert await ge.find_item_id_by_name("Noxious halberd") is None
