"""Unit tests for the AI task-generation daily quota (db/ai_task_quota.py)."""
import importlib.util
import os
import sys

import pytest

# Load by path — tests/conftest.py stubs `db` as a MagicMock, so a normal
# `from db.ai_task_quota import ...` would import the stub, not the module.
# Same pattern db.entitlements uses under the test bootstrap.
_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "db", "ai_task_quota.py")
_spec = importlib.util.spec_from_file_location("_ai_task_quota", os.path.abspath(_PATH))
quota = importlib.util.module_from_spec(_spec)
sys.modules["_ai_task_quota"] = quota
_spec.loader.exec_module(quota)


class FakeRedis:
    """Minimal INCR/EXPIRE/GET/DECR stand-in."""

    def __init__(self, initial=None, explode=False):
        self.store = dict(initial or {})
        self.expires = {}
        self.explode = explode

    def _check(self):
        if self.explode:
            raise RuntimeError("redis down")

    def get(self, key):
        self._check()
        return self.store.get(key)

    def incr(self, key):
        self._check()
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def decr(self, key):
        self._check()
        self.store[key] = int(self.store.get(key, 0)) - 1
        return self.store[key]

    def expire(self, key, seconds):
        self._check()
        self.expires[key] = seconds


@pytest.fixture
def redis(monkeypatch):
    def _install(initial=None, explode=False):
        fake = FakeRedis(initial, explode)
        monkeypatch.setattr(quota, "_rc", lambda: fake)
        return fake

    return _install


@pytest.fixture(autouse=True)
def allowance(monkeypatch):
    """Default: the tier allows 10/day. Individual tests override."""
    monkeypatch.setattr(quota, "group_allowance", lambda s, gid, **kw: 10)


class TestConsume:
    def test_charges_group_user_and_global(self, redis):
        r = redis()
        out = quota.consume(None, 7, 42)
        assert out["used"] == 1 and out["remaining"] == 9
        assert r.store["web:ratelimit:aigen:group:7"] == 1
        assert r.store["web:ratelimit:aigen:user:42"] == 1
        assert r.store["web:ratelimit:aigen:global"] == 1
        # The window is only set on the first hit of each counter.
        assert r.expires["web:ratelimit:aigen:group:7"] == 86400

    def test_group_quota_blocks_at_limit(self, redis):
        redis({"web:ratelimit:aigen:group:7": 10})
        with pytest.raises(quota.QuotaExceeded) as e:
            quota.consume(None, 7, 42)
        assert e.value.code == "ai_gen_group_quota"

    def test_user_subcap_blocks_before_group_is_exhausted(self, redis, monkeypatch):
        monkeypatch.setattr(quota, "USER_DAILY_CAP", 3)
        redis({"web:ratelimit:aigen:group:7": 1, "web:ratelimit:aigen:user:42": 3})
        with pytest.raises(quota.QuotaExceeded) as e:
            quota.consume(None, 7, 42)
        assert e.value.code == "ai_gen_user_quota"

    def test_global_circuit_breaker(self, redis, monkeypatch):
        monkeypatch.setattr(quota, "GLOBAL_DAILY_CAP", 100)
        redis({"web:ratelimit:aigen:global": 100})
        with pytest.raises(quota.QuotaExceeded) as e:
            quota.consume(None, 7, 42)
        assert e.value.code == "ai_gen_busy"

    def test_tier_without_the_feature_is_refused(self, redis, monkeypatch):
        redis()
        monkeypatch.setattr(quota, "group_allowance", lambda s, gid, **kw: 0)
        with pytest.raises(quota.QuotaExceeded) as e:
            quota.consume(None, 7, 42)
        assert e.value.code == "ai_gen_not_available"

    def test_blocked_consume_does_not_charge(self, redis):
        r = redis({"web:ratelimit:aigen:group:7": 10})
        with pytest.raises(quota.QuotaExceeded):
            quota.consume(None, 7, 42)
        assert r.store["web:ratelimit:aigen:group:7"] == 10
        assert "web:ratelimit:aigen:user:42" not in r.store

    def test_fails_open_when_redis_is_down(self, redis):
        redis(explode=True)
        out = quota.consume(None, 7, 42)
        assert out["allowed"] is True and out.get("degraded") is True

    def test_fails_open_when_redis_is_absent(self, monkeypatch):
        monkeypatch.setattr(quota, "_rc", lambda: None)
        out = quota.consume(None, 7, 42)
        assert out["allowed"] is True and out.get("degraded") is True


class TestStatus:
    def test_reports_remaining_without_charging(self, redis):
        r = redis({"web:ratelimit:aigen:group:7": 4})
        out = quota.status(None, 7, 42)
        assert out == {
            "limit": 10,
            "used": 4,
            "remaining": 6,
            "user_used": 0,
            "user_limit": quota.USER_DAILY_CAP,
            "allowed": True,
        }
        assert r.store["web:ratelimit:aigen:group:7"] == 4

    def test_remaining_is_clamped_by_the_user_subcap(self, redis, monkeypatch):
        monkeypatch.setattr(quota, "USER_DAILY_CAP", 3)
        redis({"web:ratelimit:aigen:group:7": 0, "web:ratelimit:aigen:user:42": 2})
        assert quota.status(None, 7, 42)["remaining"] == 1

    def test_not_allowed_when_the_tier_disables_it(self, redis, monkeypatch):
        redis()
        monkeypatch.setattr(quota, "group_allowance", lambda s, gid, **kw: 0)
        out = quota.status(None, 7, 42)
        assert out["allowed"] is False and out["remaining"] == 0


class TestRefund:
    def test_gives_back_one_of_each_counter(self, redis):
        r = redis({
            "web:ratelimit:aigen:group:7": 2,
            "web:ratelimit:aigen:user:42": 2,
            "web:ratelimit:aigen:global": 2,
        })
        quota.refund(7, 42)
        assert r.store["web:ratelimit:aigen:group:7"] == 1
        assert r.store["web:ratelimit:aigen:user:42"] == 1
        assert r.store["web:ratelimit:aigen:global"] == 1

    def test_never_goes_negative(self, redis):
        r = redis({"web:ratelimit:aigen:user:42": 0})
        quota.refund(7, 42)
        assert r.store["web:ratelimit:aigen:user:42"] == 0

    def test_swallows_redis_failure(self, redis):
        redis(explode=True)
        quota.refund(7, 42)  # must not raise
