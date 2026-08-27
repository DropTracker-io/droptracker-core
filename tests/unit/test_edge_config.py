"""Unit tests for services/edge_config.py — the edge Worker's runtime switch.

Loaded standalone via importlib (conftest stubs ``services`` and ``utils.redis``)
with an in-memory Redis, so these pin the one property the feature rests on:
**every ambiguous case resolves to "not mirroring"**. A missing key, unreachable
Redis, malformed JSON, a non-object payload or a nonsense sample must all mean
off. Production submissions arriving somewhere nobody expected is the failure
this is guarding against, so there is no benefit of the doubt to give.
"""
import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


ec = _load("_edge_config_under_test", "services", "edge_config.py")


class FakeRedis:
    """Just enough Redis. ttl() follows the real contract: -2 missing, -1 no expiry."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.raise_on_get = False

    def get(self, key):
        if self.raise_on_get:
            raise ConnectionError("redis is down")
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value
        self.ttls.pop(key, None)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = int(ttl)

    def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)

    def ttl(self, key):
        if key not in self.store:
            return -2
        return self.ttls.get(key, -1)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(ec, "RedisClient", lambda: SimpleNamespace(client=fake))
    return fake


class TestFailsClosed:
    """Anything other than a well-formed "on" must read as off."""

    def test_missing_key_is_disabled(self, redis):
        assert ec.mirror_config() == {"enabled": False, "sample": 1.0}

    def test_unreachable_redis_is_disabled_and_does_not_raise(self, redis):
        redis.raise_on_get = True
        assert ec.mirror_config()["enabled"] is False

    def test_malformed_json_is_disabled(self, redis):
        redis.store[ec.MIRROR_KEY] = "{not json"
        assert ec.mirror_config()["enabled"] is False

    def test_non_object_payload_is_disabled(self, redis):
        redis.store[ec.MIRROR_KEY] = json.dumps([1, 2, 3])
        assert ec.mirror_config()["enabled"] is False

    def test_bytes_values_are_decoded(self, redis):
        redis.store[ec.MIRROR_KEY] = json.dumps({"enabled": True}).encode()
        assert ec.mirror_config()["enabled"] is True

    def test_enabled_absent_means_off(self, redis):
        redis.store[ec.MIRROR_KEY] = json.dumps({"sample": 1.0})
        assert ec.mirror_config()["enabled"] is False


class TestToggle:
    def test_enable_then_read_back(self, redis):
        ec.set_mirror(True)
        assert ec.mirror_config() == {"enabled": True, "sample": 1.0}

    def test_disable_deletes_the_key(self, redis):
        ec.set_mirror(True)
        ec.set_mirror(False)
        assert ec.MIRROR_KEY not in redis.store, "the key's existence is the switch"
        assert ec.mirror_config()["enabled"] is False

    def test_ttl_is_applied_when_given(self, redis):
        ec.set_mirror(True, ttl_seconds=3600)
        assert redis.ttls[ec.MIRROR_KEY] == 3600

    def test_no_ttl_means_no_expiry(self, redis):
        ec.set_mirror(True)
        assert ec.MIRROR_KEY not in redis.ttls

    def test_expiry_is_what_turns_it_off(self, redis):
        """The auto-expiry story: nothing re-reads a flag, the key just goes."""
        ec.set_mirror(True, ttl_seconds=3600)
        assert ec.mirror_config()["enabled"] is True
        redis.delete(ec.MIRROR_KEY)  # stand-in for the TTL lapsing
        assert ec.mirror_config()["enabled"] is False


class TestSampleClamping:
    @pytest.mark.parametrize(
        "stored,expected",
        [(0.5, 0.5), (5.0, 1.0), (-1.0, 0.0), ("banana", 1.0), (None, 1.0)],
    )
    def test_stored_sample_is_clamped_on_read(self, redis, stored, expected):
        redis.store[ec.MIRROR_KEY] = json.dumps({"enabled": True, "sample": stored})
        assert ec.mirror_config()["sample"] == expected

    @pytest.mark.parametrize("given,expected", [(2.0, 1.0), (-3.0, 0.0), ("x", 1.0)])
    def test_written_sample_is_clamped(self, redis, given, expected):
        ec.set_mirror(True, sample=given)
        assert json.loads(redis.store[ec.MIRROR_KEY])["sample"] == expected


class TestMirrorState:
    """The admin-panel read. Unlike mirror_config it may raise, so the panel can
    say "Redis is unreachable" instead of a confident off nobody chose."""

    def test_reports_expiry(self, redis):
        ec.set_mirror(True, ttl_seconds=3600)
        assert ec.mirror_state()["expires_at"] is not None

    def test_no_expiry_reads_as_none(self, redis):
        ec.set_mirror(True)
        assert ec.mirror_state()["expires_at"] is None

    def test_missing_key_has_no_expiry(self, redis):
        assert ec.mirror_state() == {"enabled": False, "sample": 1.0, "expires_at": None}

    def test_propagates_redis_failure(self, redis):
        redis.raise_on_get = True
        with pytest.raises(ConnectionError):
            ec.mirror_state()


class TestEdgePayload:
    def test_version_is_derived_from_content(self):
        a = ec.edge_payload({"enabled": True, "sample": 1.0})
        b = ec.edge_payload({"enabled": True, "sample": 1.0})
        assert a["version"] == b["version"]

    def test_version_changes_when_the_config_does(self):
        off = ec.edge_payload({"enabled": False, "sample": 1.0})
        on = ec.edge_payload({"enabled": True, "sample": 1.0})
        assert off["version"] != on["version"], "a stale ETag would pin the old switch"

    def test_sample_participates_in_the_version(self):
        full = ec.edge_payload({"enabled": True, "sample": 1.0})
        tenth = ec.edge_payload({"enabled": True, "sample": 0.1})
        assert full["version"] != tenth["version"]

    def test_names_no_host(self):
        """The destination stays a wrangler var. If it ever leaks into this
        document, the admin surface gains the power to redirect submissions."""
        blob = json.dumps(ec.edge_payload({"enabled": True, "sample": 1.0}))
        assert "droptracker.io" not in blob
        assert "host" not in blob.lower()

    def test_shape_matches_what_the_worker_reads(self):
        payload = ec.edge_payload({"enabled": True, "sample": 0.25})
        assert payload["mirror"] == {"enabled": True, "sample": 0.25}
        assert isinstance(payload["version"], str)
