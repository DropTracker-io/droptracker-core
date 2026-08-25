"""Unit tests for SemanticAPI's drop-source caching.

The wiki blocklisted our old User-Agent on 2026-08-20 for hitting api.php
once per >1M drop, which silently fail-opened high-value verification for
days (the fake "Ultor ring from Casket" incident). The fix caches each item's
drop-source list for DROP_CACHE_TTL_SECONDS. Invariants under test:

* allows may be served entirely from cache (no wiki round-trip);
* a rejection derived from ANY cached data is revalidated against the live
  wiki before it is issued — the cache can delay an allow, never cause a
  false reject;
* errors (wiki unreachable) are never written to the cache;
* a broken cache degrades to the uncached path instead of breaking the check.
"""
import asyncio
import importlib.util
import json
import os

# The test suite stubs the whole `osrs_api` package as a MagicMock (see
# tests/conftest.py), so import the real SemanticAPI straight from its source
# file. semantic.py only depends on the stdlib at import time.
_SEMANTIC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "osrs_api",
    "semantic.py",
)
_spec = importlib.util.spec_from_file_location("_real_semantic_for_cache_test", _SEMANTIC_PATH)
_real_semantic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_semantic)
SemanticAPI = _real_semantic.SemanticAPI


class FakeCache:
    """Dict-backed stand-in for the redis client the real code receives."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex


class BrokenCache:
    def get(self, key):
        raise ConnectionError("redis down")

    def set(self, key, value, ex=None):
        raise ConnectionError("redis down")


class _Client:
    def __init__(self, cache):
        self.cache = cache


def _api(cache, responses):
    """SemanticAPI with a fake wiki answering per bucket-quoted item name.

    ``responses`` maps an item name to its dropsline response dict; queries
    against 'infobox_item' answer from the "infobox:<name>" key. Records every
    query in api.wiki_queries.
    """
    api = SemanticAPI(client=_Client(cache))
    api.wiki_queries = []

    async def fake_bucket_query(query):
        api.wiki_queries.append(query)
        for name, response in responses.items():
            if name.startswith("infobox:"):
                if "bucket('infobox_item')" in query and api._bucket_quote(name[8:]) in query:
                    return response
            elif "bucket('dropsline')" in query and api._bucket_quote(name) in query:
                return response
        return {"bucket": []}

    api._bucket_query = fake_bucket_query
    return api


def _run(coro):
    return asyncio.run(coro)


def _dropsline_key(name):
    return f"wiki:dropsline:v1:{name.strip().lower()}"


def test_miss_queries_wiki_and_populates_cache():
    cache = FakeCache()
    api = _api(cache, {"Tanzanite fang": {"bucket": [{"page_name": "Zulrah"}]}})
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True
    assert json.loads(cache.store[_dropsline_key("Tanzanite fang")]) == ["Zulrah"]
    assert cache.ttls[_dropsline_key("Tanzanite fang")] == SemanticAPI.DROP_CACHE_TTL_SECONDS


def test_hit_allows_without_touching_the_wiki():
    cache = FakeCache()
    cache.store[_dropsline_key("Tanzanite fang")] = json.dumps(["Zulrah"])
    api = _api(cache, {})
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True
    assert api.wiki_queries == []


def test_bytes_from_redis_decode():
    # The raw redis client returns bytes, not str.
    cache = FakeCache()
    cache.store[_dropsline_key("Tanzanite fang")] = json.dumps(["Zulrah"]).encode()
    api = _api(cache, {})
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True
    assert api.wiki_queries == []


def test_cached_reject_revalidates_live_and_still_rejects():
    # Cache and live wiki agree the NPC is wrong: reject, but only after a
    # live confirmation pass.
    cache = FakeCache()
    cache.store[_dropsline_key("Twisted bow")] = json.dumps(["Chambers of Xeric"])
    api = _api(cache, {"Twisted bow": {"bucket": [{"page_name": "Chambers of Xeric"}]}})
    assert _run(api.check_drop("Twisted bow", "Goblin")) is False
    assert any("bucket('dropsline')" in q for q in api.wiki_queries)


def test_stale_cached_reject_is_overturned_by_live_data():
    # A weekly game update added the NPC as a real source; the cache is stale.
    # The revalidation pass must rescue the drop AND refresh the cache.
    cache = FakeCache()
    cache.store[_dropsline_key("Twisted bow")] = json.dumps(["Chambers of Xeric"])
    api = _api(cache, {"Twisted bow": {"bucket": [{"page_name": "Chambers of Xeric"},
                                                  {"page_name": "New Boss"}]}})
    assert _run(api.check_drop("Twisted bow", "New Boss")) is True
    assert json.loads(cache.store[_dropsline_key("Twisted bow")]) == [
        "Chambers of Xeric", "New Boss"]


def test_uncached_reject_does_not_revalidate():
    # Nothing came from cache, so the single live pass is authoritative.
    cache = FakeCache()
    api = _api(cache, {"Twisted bow": {"bucket": [{"page_name": "Chambers of Xeric"}]}})
    assert _run(api.check_drop("Twisted bow", "Goblin")) is False
    assert sum("bucket('dropsline')" in q and api._bucket_quote("Twisted bow") in q
               for q in api.wiki_queries) == 1


def test_wiki_error_is_not_cached():
    cache = FakeCache()
    api = SemanticAPI(client=_Client(cache))

    async def failing_query(query):
        return {}  # transport/API error: no 'bucket' key

    api._bucket_query = failing_query
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True  # fail-open
    assert cache.store == {}


def test_empty_sources_are_cached_but_never_drive_a_cached_reject():
    """An empty dropsline list caches (saves the two lookups for
    never-dropped items), and the resulting 'nothing drops this' rejection
    still revalidates live because its inputs were cached."""
    cache = FakeCache()
    cache.store[_dropsline_key("Noxious halberd")] = json.dumps([])
    cache.store[_dropsline_key("Noxious halberd (uncharged)")] = json.dumps([])
    cache.store["wiki:nodrops:v1:noxious halberd"] = json.dumps({"verdict": True})
    api = _api(cache, {
        "Noxious halberd": {"bucket": []},
        "infobox:Noxious halberd": {"bucket": [{"release_date": "10 July 2024"}]},
    })
    assert _run(api.check_drop("Noxious halberd", "Rogues' Chest")) is False
    # The revalidation pass hit the live wiki (dropsline + infobox age probe).
    assert any("bucket('infobox_item')" in q for q in api.wiki_queries)


def test_broken_cache_degrades_to_uncached_check():
    api = _api(BrokenCache(), {"Tanzanite fang": {"bucket": [{"page_name": "Zulrah"}]}})
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True
    assert any("bucket('dropsline')" in q for q in api.wiki_queries)


def test_no_cache_client_still_works():
    # Clients constructed without a cache attribute (tests, scripts).
    api = SemanticAPI(client=object())
    api.wiki_queries = []

    async def fake_bucket_query(query):
        api.wiki_queries.append(query)
        return {"bucket": [{"page_name": "Zulrah"}]}

    api._bucket_query = fake_bucket_query
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True


def test_corrupt_cache_entry_falls_through_to_wiki():
    cache = FakeCache()
    cache.store[_dropsline_key("Tanzanite fang")] = "{not json"
    api = _api(cache, {"Tanzanite fang": {"bucket": [{"page_name": "Zulrah"}]}})
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True
    # And the bad entry was replaced with a good one.
    assert json.loads(cache.store[_dropsline_key("Tanzanite fang")]) == ["Zulrah"]
