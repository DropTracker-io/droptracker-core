"""Paced, retrying Discord writes (utils/discord_write.py).

The case worth pinning down is the None-on-429 one: interactions' HTTP client
gives up after three consecutive 429s by returning ``None`` instead of raising,
so a send that never happened looks like a send that did. Everything in the
recap delivery ledger is downstream of that distinction — a write recorded as
delivered when Discord dropped it is a recap the user never sees and will never
be sent again.
"""

import asyncio
import sys
import types

import pytest

# conftest stubs `interactions` as a bare MagicMock, which is not a package, so
# the module under test can't do `from interactions.client.errors import ...`.
# Register just the error types it needs — the real ones are plain exceptions.
if "interactions.client.errors" not in sys.modules:
    _errors = types.ModuleType("interactions.client.errors")

    class _HTTPException(Exception):
        def __init__(self, *args, status=None, **kw):
            super().__init__(*args)
            self.status = status

    class _RateLimited(_HTTPException):
        def __init__(self, *args, retry_after=None, **kw):
            super().__init__(*args, **kw)
            self.retry_after = retry_after

    for _name, _cls in (
        ("HTTPException", _HTTPException),
        ("RateLimited", _RateLimited),
        ("Forbidden", type("Forbidden", (_HTTPException,), {})),
        ("NotFound", type("NotFound", (_HTTPException,), {})),
        ("BadRequest", type("BadRequest", (_HTTPException,), {})),
    ):
        setattr(_errors, _name, _cls)

    _client = types.ModuleType("interactions.client")
    _client.errors = _errors
    sys.modules["interactions.client"] = _client
    sys.modules["interactions.client.errors"] = _errors

from interactions.client.errors import (  # noqa: E402
    BadRequest,
    Forbidden,
    HTTPException,
    RateLimited,
)

from utils.discord_write import DiscordWriter, RateLimiter  # noqa: E402


def _writer(**kw) -> DiscordWriter:
    # Near-zero pacing: these tests are about control flow, not wall clock.
    params = dict(
        label="TEST",
        global_max_calls=100, global_period=0.01,
        bucket_max_calls=100, bucket_period=0.01,
        backoff_scale=0.002,
    )
    params.update(kw)
    return DiscordWriter(**params)


class TestRateLimiter:
    async def test_allows_up_to_max_calls_without_waiting(self):
        limiter = RateLimiter(max_calls=3, period_seconds=10.0)
        start = asyncio.get_event_loop().time()
        for _ in range(3):
            await limiter.acquire()
        assert asyncio.get_event_loop().time() - start < 0.5

    async def test_blocks_once_the_window_is_full(self):
        limiter = RateLimiter(max_calls=1, period_seconds=0.25)
        await limiter.acquire()
        start = asyncio.get_event_loop().time()
        await limiter.acquire()
        # Second call must have waited out the window rather than sailing past.
        assert asyncio.get_event_loop().time() - start >= 0.2


class TestWrite:
    async def test_returns_the_factory_result(self):
        w = _writer()
        assert await w.write("bucket", lambda: _value("sent")) == "sent"

    async def test_factory_is_called_fresh_each_attempt(self):
        # A coroutine can only be awaited once, so a retry needs a new one —
        # passing a coroutine instead of a factory would raise on retry.
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise HTTPException("boom")
            return "ok"

        w = _writer()
        assert await w.write("bucket", flaky) == "ok"
        assert len(calls) == 3

    async def test_none_is_success_when_no_result_is_expected(self):
        # Deletes legitimately return None.
        w = _writer()
        assert await w.write("bucket", lambda: _value(None)) is None

    async def test_none_is_retried_when_a_result_is_expected(self):
        attempts = []

        async def silently_ratelimited():
            attempts.append(1)
            return None if len(attempts) < 2 else "message"

        w = _writer()
        got = await w.write("bucket", silently_ratelimited, expect_result=True)
        assert got == "message"
        assert len(attempts) == 2

    async def test_persistent_none_raises_rather_than_reporting_success(self):
        # The whole point: the caller must not record a delivery for this.
        w = _writer(attempts=2)
        with pytest.raises(RuntimeError):
            await w.write("bucket", lambda: _value(None), expect_result=True)

    async def test_forbidden_propagates_without_retrying(self):
        attempts = []

        async def closed_dms():
            attempts.append(1)
            raise Forbidden("cannot send to this user")

        w = _writer()
        with pytest.raises(Forbidden):
            await w.write("bucket", closed_dms)
        assert len(attempts) == 1

    async def test_bad_request_fails_fast(self):
        attempts = []

        async def malformed():
            attempts.append(1)
            raise BadRequest("invalid embed")

        w = _writer()
        with pytest.raises(BadRequest):
            await w.write("bucket", malformed)
        assert len(attempts) == 1

    async def test_rate_limited_is_retried_then_succeeds(self):
        attempts = []

        async def throttled():
            attempts.append(1)
            if len(attempts) == 1:
                raise RateLimited("slow down")
            return "message"

        w = _writer()
        assert await w.write("bucket", throttled, expect_result=True) == "message"
        assert len(attempts) == 2

    async def test_exhausted_retries_reraise_the_last_error(self):
        async def always_down():
            raise HTTPException("503")

        w = _writer(attempts=2)
        with pytest.raises(HTTPException):
            await w.write("bucket", always_down)

    async def test_buckets_are_independent(self):
        w = _writer(bucket_max_calls=1, bucket_period=10.0)
        await w.write("a", lambda: _value(1))
        start = asyncio.get_event_loop().time()
        await w.write("b", lambda: _value(2))
        # A different channel/recipient must not queue behind the first.
        assert asyncio.get_event_loop().time() - start < 0.5
        assert w.limiter_for("a") is not w.limiter_for("b")


async def _value(v):
    return v
