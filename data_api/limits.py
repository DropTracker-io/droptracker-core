"""Rate limiting, keyed on the API key.

Deliberately not ``quart-rate-limiter``, which this codebase already uses on
the intake API: its default store is in-memory *per worker*, so a limit of 30
is really 30xN workers unevenly, and its default key is
``request.access_route[0]`` — the leftmost ``X-Forwarded-For`` entry, which the
client supplies and can therefore forge. Neither is acceptable for a
credentialed external API. Here the counters live in Redis (shared by every
worker) and the identity is the authenticated ``key_id``, which cannot be
spoofed without the secret.

Three budgets, all fixed-window ``INCR``+``EXPIRE`` counters in the
``dataapi:rl:*`` namespace (the convention already used by
``web:ratelimit:*``), plus a concurrency gate:

    requests/min     — burst control
    cost units/min   — the real work budget; a cheap call and a 100-player
                       clog_slots dump both cost one *request* but differ by
                       three orders of magnitude in actual load
    requests/day     — sustained-volume ceiling
    max_concurrency  — how many requests one key may have in flight, so a
                       single caller cannot occupy every worker

**Cost is charged before the work is done**, from the declared sections and
page size, so an expensive request is refused rather than executed and then
billed. That is the whole point: the budget has to bite before the database
does the work, not after.

Fails **open** on a Redis error — matching every other limiter in this
codebase. An API that stops serving because the counter store blinked is worse
than one that briefly stops counting, but it is logged loudly either way.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

NAMESPACE = "dataapi:rl"
#: Safety TTL on the concurrency counter: a process killed mid-request must not
#: leak a permanently-held slot.
_CONCURRENCY_TTL = 120


def _redis():
    try:
        from utils.redis import RedisClient

        return RedisClient().client
    except Exception:
        return None


class LimitDecision:
    """Whether a request may proceed, and the headers describing why."""

    def __init__(self, allowed: bool, headers: dict,
                 retry_after: Optional[int] = None, reason: str = ""):
        self.allowed = allowed
        self.headers = headers
        self.retry_after = retry_after
        self.reason = reason


def check_and_charge(key_id: int, limits: dict, cost: int) -> LimitDecision:
    """Charge ``cost`` against ``key_id``'s budgets, or refuse.

    Counters are incremented first and compared afterwards. That can overshoot
    a window by the requests already in flight, which is the right trade here:
    the alternative (check, then increment) lets concurrent requests all pass
    the check before any of them counts, which is unbounded rather than
    bounded overshoot.
    """
    conn = _redis()
    now = int(time.time())
    minute, day = now // 60, time.strftime("%Y%m%d", time.gmtime(now))

    req_limit = int(limits["requests_per_min"])
    cost_limit = int(limits["cost_units_per_min"])
    day_limit = int(limits["requests_per_day"])

    if conn is None:
        return LimitDecision(True, {
            "X-RateLimit-Limit": str(req_limit),
            "X-RateLimit-Remaining": str(req_limit),
            "X-RateLimit-Reset": str((minute + 1) * 60),
            "X-RateLimit-Cost": str(cost),
        }, reason="redis_unavailable")

    minute_key = f"{NAMESPACE}:{key_id}:req:{minute}"
    cost_key = f"{NAMESPACE}:{key_id}:cost:{minute}"
    day_key = f"{NAMESPACE}:{key_id}:day:{day}"

    try:
        pipe = conn.pipeline()
        pipe.incr(minute_key, 1)
        pipe.expire(minute_key, 120)
        pipe.incrby(cost_key, cost)
        pipe.expire(cost_key, 120)
        pipe.incr(day_key, 1)
        pipe.expire(day_key, 172800)
        results = pipe.execute()
        used_requests, used_cost, used_day = results[0], results[2], results[4]
    except Exception:
        return LimitDecision(True, {
            "X-RateLimit-Limit": str(req_limit),
            "X-RateLimit-Remaining": str(req_limit),
            "X-RateLimit-Reset": str((minute + 1) * 60),
            "X-RateLimit-Cost": str(cost),
        }, reason="redis_error")

    reset_at = (minute + 1) * 60
    headers = {
        "X-RateLimit-Limit": str(req_limit),
        "X-RateLimit-Remaining": str(max(0, req_limit - int(used_requests))),
        "X-RateLimit-Reset": str(reset_at),
        "X-RateLimit-Cost": str(cost),
        "X-RateLimit-Cost-Limit": str(cost_limit),
        "X-RateLimit-Cost-Remaining": str(max(0, cost_limit - int(used_cost))),
    }

    if int(used_requests) > req_limit:
        return LimitDecision(False, headers, max(1, reset_at - now), "requests_per_min")
    if int(used_cost) > cost_limit:
        return LimitDecision(False, headers, max(1, reset_at - now), "cost_units_per_min")
    if int(used_day) > day_limit:
        midnight = (now // 86400 + 1) * 86400
        return LimitDecision(False, headers, max(1, midnight - now), "requests_per_day")
    return LimitDecision(True, headers)


class Concurrency:
    """Context manager holding one in-flight slot for a key.

    ``acquired`` is False when the key is already at its ceiling; the caller
    turns that into a 429. Release is unconditional, so an exception inside the
    request still gives the slot back.
    """

    def __init__(self, key_id: int, ceiling: int):
        self.key = f"{NAMESPACE}:{key_id}:conc"
        self.ceiling = int(ceiling)
        self.acquired = False
        self._conn = None

    def __enter__(self):
        self._conn = _redis()
        if self._conn is None:
            self.acquired = True  # fail open
            return self
        try:
            current = self._conn.incr(self.key, 1)
            self._conn.expire(self.key, _CONCURRENCY_TTL)
            if int(current) > self.ceiling:
                self._conn.decr(self.key, 1)
                self.acquired = False
            else:
                self.acquired = True
        except Exception:
            self.acquired = True
        return self

    def __exit__(self, *_exc):
        if self._conn is not None and self.acquired:
            try:
                self._conn.decr(self.key, 1)
            except Exception:
                pass
        return False
