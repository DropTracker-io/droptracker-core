"""Per-key usage metering.

A separate namespace (``dataapi:usage:*``) from the intake API's ``metrics:*``
on purpose: that tracker records ``(type, success, app)`` with no per-caller,
per-endpoint or latency dimension, has no TTL on its all-time keys, and its
``/metrics`` endpoint currently has no consumer in the repo. Rather than widen
something unused, this records exactly the dimensions the question "who is
making us slow, and where?" needs:

    requests, errors, total cost, total + max latency, per endpoint, per key.

Minute buckets answer "what is happening right now" and expire after two
hours; hour buckets answer "who has been expensive today" and expire after
eight days. Anything longer belongs in a rollup table, not Redis.

Recording is best-effort and must never fail a request that already succeeded:
every write is wrapped, and a metering outage costs visibility, not service.
"""
from __future__ import annotations

import time
from typing import Optional

NAMESPACE = "dataapi:usage"
_MINUTE_TTL = 7200        # 2h
_HOUR_TTL = 691200        # 8d
#: Requests slower than this are worth naming individually.
SLOW_REQUEST_MS = 1000


def _redis():
    try:
        from utils.redis import RedisClient

        return RedisClient().client
    except Exception:
        return None


def record(key_id: int, endpoint: str, status: int, duration_ms: float,
           cost: int, players: int = 1, limited: bool = False) -> None:
    """Fold one finished request into the minute and hour buckets."""
    conn = _redis()
    if conn is None:
        return

    now = int(time.time())
    minute, hour = now // 60, now // 3600
    bucket_min = f"{NAMESPACE}:m:{minute}"
    bucket_hour = f"{NAMESPACE}:h:{hour}"
    key_hour = f"{NAMESPACE}:key:{key_id}:h:{hour}"
    duration = int(duration_ms)

    try:
        pipe = conn.pipeline()
        for bucket, ttl in ((bucket_min, _MINUTE_TTL), (bucket_hour, _HOUR_TTL)):
            pipe.hincrby(bucket, "requests", 1)
            pipe.hincrby(bucket, "cost", cost)
            pipe.hincrby(bucket, "duration_ms", duration)
            pipe.hincrby(bucket, f"endpoint:{endpoint}", 1)
            pipe.hincrby(bucket, f"status:{status // 100}xx", 1)
            if limited:
                pipe.hincrby(bucket, "limited", 1)
            pipe.expire(bucket, ttl)

        # Per-key detail: what this consumer costs and where it spends it.
        pipe.hincrby(key_hour, "requests", 1)
        pipe.hincrby(key_hour, "cost", cost)
        pipe.hincrby(key_hour, "players", players)
        pipe.hincrby(key_hour, "duration_ms", duration)
        pipe.hincrby(key_hour, f"endpoint:{endpoint}", 1)
        pipe.hincrby(key_hour, f"status:{status // 100}xx", 1)
        if limited:
            pipe.hincrby(key_hour, "limited", 1)
        if duration >= SLOW_REQUEST_MS:
            pipe.hincrby(key_hour, "slow", 1)
        pipe.expire(key_hour, _HOUR_TTL)

        # Max latency needs a read-compare-write; a lost race here costs one
        # sample of a maximum, which is not worth a lock.
        pipe.hget(key_hour, "max_ms")
        results = pipe.execute()
        previous = results[-1]
        if previous is None or duration > int(previous):
            conn.hset(key_hour, "max_ms", duration)

        # The active-keys index, so the dashboard can enumerate without SCAN.
        conn.sadd(f"{NAMESPACE}:keys:h:{hour}", key_id)
        conn.expire(f"{NAMESPACE}:keys:h:{hour}", _HOUR_TTL)
    except Exception:
        return


def touch_last_used(key_id: int) -> bool:
    """True at most once a minute per key — the caller then writes the column.

    ``api_keys.last_used_at`` is a liveness hint, not an audit trail, and is
    not worth an UPDATE on every request.
    """
    conn = _redis()
    if conn is None:
        return False
    try:
        return bool(conn.set(f"{NAMESPACE}:touch:{key_id}", "1", ex=60, nx=True))
    except Exception:
        return False


def read_window(hours: int = 24) -> dict:
    """Aggregate the last ``hours`` hour-buckets — what the dashboard reads."""
    conn = _redis()
    if conn is None:
        return {"available": False, "hours": hours, "totals": {}, "keys": []}

    now = int(time.time())
    current_hour = now // 3600
    hour_range = [current_hour - offset for offset in range(hours)]

    totals = {"requests": 0, "cost": 0, "duration_ms": 0, "limited": 0}
    endpoints: dict = {}
    statuses: dict = {}
    key_ids = set()

    try:
        pipe = conn.pipeline()
        for hour in hour_range:
            pipe.hgetall(f"{NAMESPACE}:h:{hour}")
        for hour in hour_range:
            pipe.smembers(f"{NAMESPACE}:keys:h:{hour}")
        results = pipe.execute()

        for bucket in results[:len(hour_range)]:
            for field, value in (bucket or {}).items():
                field = field.decode() if isinstance(field, bytes) else field
                value = int(value)
                if field in totals:
                    totals[field] += value
                elif field.startswith("endpoint:"):
                    endpoints[field[9:]] = endpoints.get(field[9:], 0) + value
                elif field.startswith("status:"):
                    statuses[field[7:]] = statuses.get(field[7:], 0) + value

        for members in results[len(hour_range):]:
            for member in members or ():
                key_ids.add(int(member))

        per_key = []
        if key_ids:
            pipe = conn.pipeline()
            ordered = sorted(key_ids)
            for key_id in ordered:
                for hour in hour_range:
                    pipe.hgetall(f"{NAMESPACE}:key:{key_id}:h:{hour}")
            raw = pipe.execute()
            for index, key_id in enumerate(ordered):
                slice_ = raw[index * len(hour_range):(index + 1) * len(hour_range)]
                summary = {"key_id": key_id, "requests": 0, "cost": 0,
                           "players": 0, "duration_ms": 0, "slow": 0,
                           "limited": 0, "max_ms": 0, "errors": 0}
                for bucket in slice_:
                    for field, value in (bucket or {}).items():
                        field = field.decode() if isinstance(field, bytes) else field
                        value = int(value)
                        if field == "max_ms":
                            summary["max_ms"] = max(summary["max_ms"], value)
                        elif field in ("status:4xx", "status:5xx"):
                            summary["errors"] += value
                        elif field in summary:
                            summary[field] += value
                if summary["requests"]:
                    summary["avg_ms"] = round(summary["duration_ms"] / summary["requests"], 1)
                    per_key.append(summary)
            per_key.sort(key=lambda s: s["cost"], reverse=True)
    except Exception:
        return {"available": False, "hours": hours, "totals": {}, "keys": []}

    if totals["requests"]:
        totals["avg_ms"] = round(totals["duration_ms"] / totals["requests"], 1)
    return {
        "available": True,
        "hours": hours,
        "totals": totals,
        "endpoints": dict(sorted(endpoints.items(), key=lambda kv: -kv[1])),
        "statuses": statuses,
        "keys": per_key,
    }
