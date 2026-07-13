"""Global seasonal-processing kill switch.

Superadmins toggle this from the web admin panel (web_api/routes/admin.py).
When OFF, every intake path (api/routes/webhook.py, workers/webhook_consumer.py,
bots/webhook_bot.py) skips seasonal-world submissions entirely instead of
running the seasonal processors — used between Leagues/Deadman seasons so the
pipeline does no unnecessary work.

Source of truth is the Redis key ``seasonal:active`` ("1"/"0"). A missing key
means ACTIVE (preserves the pre-kill-switch behavior). Readers cache the value
in-process for a short TTL so the hot path costs nothing.
"""

import time

from utils.redis import RedisClient

ACTIVE_KEY = "seasonal:active"
_CACHE_TTL_SECONDS = 30.0

_cache = {"value": None, "ts": 0.0}


def _coerce(raw) -> bool:
    if isinstance(raw, bytes):
        raw = raw.decode(errors="ignore")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def is_seasonal_active(default: bool = True) -> bool:
    """True when seasonal-world submissions should be processed.

    Best-effort: on Redis errors the last known value (or ``default``) is
    returned — the kill switch must never break intake.
    """
    now = time.monotonic()
    if _cache["value"] is not None and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["value"]
    try:
        raw = RedisClient().client.get(ACTIVE_KEY)
        value = default if raw is None else _coerce(raw)
    except Exception:
        value = _cache["value"] if _cache["value"] is not None else default
    _cache["value"] = value
    _cache["ts"] = now
    return value


def set_seasonal_active(active: bool) -> None:
    """Persist the switch (raises on Redis failure so callers can surface it)."""
    RedisClient().client.set(ACTIVE_KEY, "1" if active else "0")
    _cache["value"] = bool(active)
    _cache["ts"] = time.monotonic()
