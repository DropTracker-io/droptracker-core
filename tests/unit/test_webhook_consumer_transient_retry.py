"""Infrastructure faults must not dead-letter an accepted submission.

On 2026-08-02 MariaDB was OOM-killed for ~7 seconds. The webhook consumer
caught the resulting OperationalError with a bare `except Exception` and sent
36 already-ACCEPTED submissions — including two personal bests — straight to
the dead list, where nothing would ever have looked at them again. The API had
already told those plugins their submission was accepted.

The events consumer had solved this months earlier with `_is_retryable`; this
pins the same split here. The two halves both matter: classing a bad payload as
transient turns a poison message into an infinite loop, and classing a DB
restart as poison silently loses real player data.
"""

import json
import sys
from unittest.mock import MagicMock

import pytest

import workers.webhook_consumer as wc


class _FakeRedis:
    def __init__(self):
        self.pushed = []

    def rpush(self, key, value):
        self.pushed.append((key, value))
        return 1


class TestRetryClassification:
    def test_database_restart_is_retryable(self):
        from sqlalchemy.exc import OperationalError

        # The exact shape of the 2026-08-02 OOM: connection refused.
        assert wc._is_retryable(OperationalError("stmt", {}, Exception("refused")))

    def test_lost_connection_is_retryable(self):
        from sqlalchemy.exc import InterfaceError

        assert wc._is_retryable(InterfaceError("stmt", {}, Exception("gone away")))

    def test_redis_trouble_is_retryable(self):
        import redis

        assert wc._is_retryable(redis.RedisError("no connection"))

    def test_socket_errors_are_retryable(self):
        assert wc._is_retryable(ConnectionRefusedError())
        assert wc._is_retryable(OSError("network unreachable"))

    def test_a_bad_payload_is_not_retryable(self):
        # Poison must still dead-letter, or one malformed envelope loops forever.
        assert not wc._is_retryable(ValueError("malformed embed"))
        assert not wc._is_retryable(KeyError("player_name"))

    def test_a_programming_error_is_not_retryable(self):
        assert not wc._is_retryable(AttributeError("'NoneType' has no attribute 'id'"))


class TestRequeueEscalation:
    def _entry(self, **extra):
        return json.dumps({"payload": {"type": "pb"}, **extra}).encode()

    def test_first_failure_is_requeued_with_a_counter(self):
        r = _FakeRedis()
        assert wc._requeue_with_backoff(r, self._entry()) is True
        key, value = r.pushed[0]
        assert key == wc.QUEUE_KEY
        assert json.loads(value)["_attempts"] == 1

    def test_the_counter_rides_on_the_envelope(self):
        """It must survive a consumer restart — an in-memory counter would
        reset exactly when a crash loop is doing the most damage."""
        r = _FakeRedis()
        entry = self._entry()
        for expected in (1, 2, 3, 4):
            assert wc._requeue_with_backoff(r, entry) is True
            entry = r.pushed[-1][1].encode()
            assert json.loads(entry)["_attempts"] == expected

    def test_it_gives_up_at_the_cap(self):
        r = _FakeRedis()
        entry = self._entry(_attempts=wc.RETRY_MAX_ATTEMPTS - 1)
        # Spent: the caller dead-letters instead, so a permanently broken
        # envelope cannot cycle forever.
        assert wc._requeue_with_backoff(r, entry) is False
        assert r.pushed == []

    def test_unparseable_bytes_are_not_requeued(self):
        r = _FakeRedis()
        assert wc._requeue_with_backoff(r, b"{not json") is False
        assert r.pushed == []

    def test_a_redis_failure_falls_back_to_dead_lettering(self):
        class _Broken:
            def rpush(self, *a, **k):
                raise RuntimeError("redis down")

        assert wc._requeue_with_backoff(_Broken(), self._entry()) is False
