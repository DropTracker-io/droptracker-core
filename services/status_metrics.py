"""Lightweight Redis metrics powering the #status channel and /admin/status.

Writers sit on intake/processing paths, so every function here is fail-open:
Redis being down must never break submission processing.

Keyspace:
  status:metrics:{source}:m:{minute}  processed-submission counter per epoch
                                      minute, TTL 25h (24h window + slack)
  status:metrics:{source}:players     ZSET player_key -> last-seen unix ts;
                                      trimmed on read, TTL refreshed on write
  status:heartbeat:{service}          unix ts of last liveness signal, TTL 180s
  status:issues:rev                   monotonic counter bumped by the web API
                                      when known issues change (bot re-renders)

Sources: "api" (plugin -> POST /webhook -> webhook:queue -> consumer) and
"webhook" (legacy Discord-webhook reader bot). Heartbeat services:
"webhook_consumer", "webhook_bot".
"""

from __future__ import annotations

import os
import time
from typing import Optional

SOURCE_API = "api"
SOURCE_WEBHOOK = "webhook"

HEARTBEAT_CONSUMER = "webhook_consumer"
HEARTBEAT_WEBHOOK_BOT = "webhook_bot"

WINDOWS = {"5m": 5, "30m": 30, "24h": 1440}

_MINUTE_TTL = 25 * 3600
_PLAYERS_TTL = 26 * 3600
HEARTBEAT_TTL = 180

ISSUES_REV_KEY = "status:issues:rev"


def _conn(r=None):
    """Accept a raw redis.Redis, a RedisClient wrapper, or nothing (singleton).

    A raw redis.Redis exposes ``pipeline`` (and has a ``client`` *method*, so
    ``getattr(r, "client", ...)`` must not be the wrapper test); the in-house
    RedisClient wrapper exposes neither ``pipeline`` nor callable ``client``.
    """
    if r is not None:
        if hasattr(r, "pipeline"):
            return r
        return getattr(r, "client", None)
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def player_key(*candidates) -> Optional[str]:
    """First usable identity string, normalized. Name preferred over hash."""
    for c in candidates:
        if c is None:
            continue
        s = str(c).strip().lower()
        if s:
            return s[:64]
    return None


def _counter_key(source: str, minute: int) -> str:
    return f"status:metrics:{source}:m:{minute}"


def _players_key(source: str) -> str:
    return f"status:metrics:{source}:players"


def _heartbeat_key(service: str) -> str:
    return f"status:heartbeat:{service}"


def record_processed(source: str, player: Optional[str] = None, *, count: int = 1,
                     r=None, now: Optional[float] = None) -> None:
    """Count a successfully processed submission (and mark its player active)."""
    conn = _conn(r)
    if conn is None:
        return
    try:
        ts = now if now is not None else time.time()
        key = _counter_key(source, int(ts // 60))
        pipe = conn.pipeline(transaction=False)
        pipe.incrby(key, count)
        pipe.expire(key, _MINUTE_TTL)
        if player:
            pkey = _players_key(source)
            pipe.zadd(pkey, {player: int(ts)})
            pipe.expire(pkey, _PLAYERS_TTL)
        pipe.execute()
    except Exception:
        pass


def heartbeat(service: str, *, r=None, now: Optional[float] = None) -> None:
    conn = _conn(r)
    if conn is None:
        return
    try:
        ts = int(now if now is not None else time.time())
        conn.setex(_heartbeat_key(service), HEARTBEAT_TTL, str(ts))
    except Exception:
        pass


def get_processed_counts(source: str, *, r=None, now: Optional[float] = None) -> dict:
    """Sums of the minute buckets: {"5m": n, "30m": n, "24h": n} (fail-open zeros)."""
    zeros = {label: 0 for label in WINDOWS}
    conn = _conn(r)
    if conn is None:
        return zeros
    try:
        minute = int((now if now is not None else time.time()) // 60)
        span = max(WINDOWS.values())
        vals = conn.mget([_counter_key(source, minute - i) for i in range(span)])

        def _sum(n: int) -> int:
            return sum(int(v) for v in vals[:n] if v)

        return {label: _sum(n) for label, n in WINDOWS.items()}
    except Exception:
        return zeros


def get_active_players(source: str, *, window_seconds: int = 3600, r=None,
                       now: Optional[float] = None) -> int:
    """Distinct players seen in the window. Trims stale entries as it reads."""
    conn = _conn(r)
    if conn is None:
        return 0
    try:
        ts = int(now if now is not None else time.time())
        pkey = _players_key(source)
        pipe = conn.pipeline(transaction=False)
        pipe.zremrangebyscore(pkey, "-inf", ts - window_seconds)
        pipe.zcard(pkey)
        return int(pipe.execute()[1] or 0)
    except Exception:
        return 0


def get_heartbeat_age(service: str, *, r=None, now: Optional[float] = None) -> Optional[int]:
    """Seconds since the service last heartbeat, or None if no fresh beat."""
    conn = _conn(r)
    if conn is None:
        return None
    try:
        raw = conn.get(_heartbeat_key(service))
        if raw is None:
            return None
        ts = int(now if now is not None else time.time())
        return max(0, ts - int(raw))
    except Exception:
        return None


def get_queue_depth(*, r=None) -> Optional[int]:
    """Depth of the fast-accept intake queue (None = unknown)."""
    conn = _conn(r)
    if conn is None:
        return None
    try:
        return int(conn.llen("webhook:queue"))
    except Exception:
        return None


API_PING_URL = os.getenv("STATUS_API_PING_URL", "http://127.0.0.1:31323/ping")
# Above this many queued envelopes the API card reads "Degraded" — intake is
# accepting but processing is behind.
QUEUE_BACKLOG_DEGRADED = int(os.getenv("STATUS_QUEUE_BACKLOG_DEGRADED", "500"))

STATUS_OPERATIONAL = "operational"
STATUS_DEGRADED = "degraded"
STATUS_OFFLINE = "offline"


def ping_api(timeout: float = 2.0) -> bool:
    """Blocking HTTP liveness probe of the intake API (call via to_thread)."""
    try:
        import urllib.request

        req = urllib.request.Request(API_PING_URL, headers={"User-Agent": "dt-status"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def collect_service_snapshot(*, r=None, now: Optional[float] = None) -> dict:
    """Everything the status card needs, in one blocking call (use to_thread).

    Status semantics:
      api      operational = HTTP up, consumer beating, queue not backed up
               degraded    = HTTP up but consumer stale or backlog large
               offline     = HTTP probe failed
      webhook  operational = reader bot heartbeat fresh; offline otherwise
    """
    ts = int(now if now is not None else time.time())
    queue_depth = get_queue_depth(r=r)
    consumer_age = get_heartbeat_age(HEARTBEAT_CONSUMER, r=r, now=ts)
    webhook_age = get_heartbeat_age(HEARTBEAT_WEBHOOK_BOT, r=r, now=ts)
    api_online = ping_api()

    if not api_online:
        api_status = STATUS_OFFLINE
    elif consumer_age is None or (queue_depth or 0) >= QUEUE_BACKLOG_DEGRADED:
        api_status = STATUS_DEGRADED
    else:
        api_status = STATUS_OPERATIONAL

    return {
        "generated_at": ts,
        "api": {
            "status": api_status,
            "online": bool(api_online),
            "players_1h": get_active_players(SOURCE_API, r=r, now=ts),
            "processed": get_processed_counts(SOURCE_API, r=r, now=ts),
            "queue_depth": queue_depth,
            "consumer_alive": consumer_age is not None,
        },
        "webhook": {
            "status": STATUS_OPERATIONAL if webhook_age is not None else STATUS_OFFLINE,
            "online": webhook_age is not None,
            "players_1h": get_active_players(SOURCE_WEBHOOK, r=r, now=ts),
            "processed": get_processed_counts(SOURCE_WEBHOOK, r=r, now=ts),
        },
    }


def bump_issues_rev(*, r=None) -> None:
    conn = _conn(r)
    if conn is None:
        return
    try:
        conn.incr(ISSUES_REV_KEY)
    except Exception:
        pass


def get_issues_rev(*, r=None) -> int:
    conn = _conn(r)
    if conn is None:
        return 0
    try:
        return int(conn.get(ISSUES_REV_KEY) or 0)
    except Exception:
        return 0
