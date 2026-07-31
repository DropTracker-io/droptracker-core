"""Paced, retrying Discord writes.

Extracted from ``services/hall_of_fame.py``, which learned all of this the hard
way during issue #31 and was the only place in the codebase that got it right.
It lives here because the monthly recap delivery has the same problem at a
larger scale — one card per clan channel plus a DM per opted-in user, in a burst
— and a second hand-rolled copy would drift from this one.

Two things this exists to prevent.

**Silent rate-limit exhaustion.** interactions' HTTPClient gives up after three
consecutive 429s by *ending its request loop and returning ``None``* rather than
raising. A ``send()`` that returns None looks like success to any caller that
doesn't check, so messages vanish while the code records them as delivered. Any
call that should produce a Message must pass ``expect_result=True``; a None then
means "rate limited into oblivion", is waited out, retried, and finally raised
so the caller does not write a delivery row for a message nobody received.

**Bursts against a per-channel bucket.** Discord's per-channel write bucket is
small and shared with everything else the bot is doing. Every write goes through
a global limiter and a per-bucket limiter, both sliding-window, so a thousand-DM
run paces itself instead of discovering the limit by hitting it.

Failure taxonomy is deliberate: ``Forbidden``/``NotFound`` propagate immediately
(a closed DM or a deleted channel is the caller's business, and retrying makes
it worse), ``BadRequest`` fails fast because retrying a malformed payload can
never succeed, and everything else backs off and retries.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Awaitable, Callable, Dict, Optional

from interactions.client.errors import (
    BadRequest,
    Forbidden,
    HTTPException,
    NotFound,
    RateLimited,
)

log = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window limiter: at most ``max_calls`` per ``period_seconds``."""

    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period = period_seconds
        self.calls: deque = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > self.period:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                sleep_for = self.period - (now - self.calls[0])
                await asyncio.sleep(max(sleep_for, 0.0) + random.uniform(0.01, 0.05))


class DiscordWriter:
    """Runs Discord writes with pacing, retries and honest failure.

    ``bucket`` is whatever the per-target limit applies to — a channel id for
    channel posts, a recipient id for DMs. Buckets are created on demand and
    kept, which is fine at our cardinality (hundreds of channels, low thousands
    of recipients per monthly run) but means a long-lived writer holds a small
    amount of memory per distinct target.
    """

    def __init__(
        self,
        *,
        label: str,
        global_max_calls: int = 6,
        global_period: float = 1.0,
        bucket_max_calls: int = 1,
        bucket_period: float = 1.5,
        attempts: int = 4,
        backoff_scale: float = 1.0,
    ):
        self.label = label
        self.attempts = attempts
        # Multiplier on every retry sleep. Production leaves this at 1.0; tests
        # shrink it so exercising the retry paths costs milliseconds rather than
        # the ~40s of real backoff those paths are designed to spend.
        self.backoff_scale = backoff_scale
        self._bucket_max_calls = bucket_max_calls
        self._bucket_period = bucket_period
        self._global_limiter = RateLimiter(global_max_calls, global_period)
        self._bucket_limiters: Dict[str, RateLimiter] = {}

    def limiter_for(self, bucket: str) -> RateLimiter:
        limiter = self._bucket_limiters.get(bucket)
        if limiter is None:
            limiter = RateLimiter(self._bucket_max_calls, self._bucket_period)
            self._bucket_limiters[bucket] = limiter
        return limiter

    async def write(
        self,
        bucket: str,
        factory: Callable[[], Awaitable[Any]],
        expect_result: bool = False,
    ) -> Any:
        """Run one Discord write with pacing and retries.

        ``factory`` is a zero-arg callable returning a *fresh* coroutine — a
        coroutine object can only be awaited once, so a retry needs a new one.

        Pass ``expect_result=True`` for anything that should return a Message.
        See the module docstring for why that is not optional.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.attempts + 1):
            await self._global_limiter.acquire()
            await self.limiter_for(bucket).acquire()
            try:
                result = await factory()
                if expect_result and result is None:
                    delay = (5.0 * attempt + random.uniform(0.5, 1.5)) * self.backoff_scale
                    log.warning(
                        "%s: write on %s returned None (library gave up on 429s), "
                        "sleeping %.1fs before retry (attempt %d)",
                        self.label, bucket, delay, attempt,
                    )
                    await asyncio.sleep(delay)
                    continue
                return result
            except (Forbidden, NotFound, BadRequest):
                raise
            except RateLimited as e:
                retry_after = float(getattr(e, "retry_after", None) or 2.0)
                delay = (min(retry_after, 60.0) + random.uniform(0.1, 0.5)) * self.backoff_scale
                log.warning(
                    "%s: 429 on %s, sleeping %.1fs (attempt %d)",
                    self.label, bucket, delay, attempt,
                )
                last_exc = e
                await asyncio.sleep(delay)
            except HTTPException as e:
                last_exc = e
                delay = (min(2.0 * attempt, 8.0) + random.uniform(0.1, 0.5)) * self.backoff_scale
                log.warning(
                    "%s: HTTP %s on %s, retrying in %.1fs (attempt %d)",
                    self.label, getattr(e, "status", "?"), bucket, delay, attempt,
                )
                await asyncio.sleep(delay)
        if last_exc:
            raise last_exc
        raise RuntimeError(
            f"{self.label}: discord write on {bucket} kept returning None (rate limited)"
        )
