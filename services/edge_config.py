"""Runtime configuration for the Cloudflare edge Worker (edge/intake-capture).

Today this carries one thing: whether the Worker also mirrors production
submissions at the dev instance. Superadmins toggle it from the web admin panel
(web_api/routes/admin.py); the Worker learns it by polling ``GET /edge-config``
on the intake API, which reads the state written here.

Two deliberate differences from services/seasonal_state.py, which this otherwise
copies:

*   **It fails closed.** A missing key, an unreachable Redis or a malformed
    value all mean "not mirroring". Seasonal fails open because refusing to
    process submissions is the damaging outcome there; here the damaging outcome
    is production traffic arriving somewhere nobody expected it, so the
    ambiguous cases have to resolve to off.

*   **The key's existence *is* the switch.** Enabling writes it (with a TTL);
    disabling deletes it. That makes auto-expiry mean exactly what it looks
    like — the key lapsing is the feature turning itself off — rather than
    leaving a separate stored flag that could disagree with the TTL.

There is no in-process memo. Nothing on the submission hot path reads this: the
only consumer is /edge-config, which Cloudflare edge-caches for 30s, so the
real query rate is a handful per minute and a cached value would only add
latency to the toggle taking effect.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from utils.redis import RedisClient

MIRROR_KEY = "edge:mirror"

#: What every failure path resolves to.
DISABLED = {"enabled": False, "sample": 1.0}

#: Offered by the admin panel. Unbounded is possible but deliberately not a
#: default — mirroring is a debugging mode, and one left on for a week is how
#: the dev box quietly fills its disk.
TTL_CHOICES = (3600, 4 * 3600, 24 * 3600)


def _coerce(raw) -> dict:
    """Parse a stored value into a config, or DISABLED if it is not one."""
    if isinstance(raw, bytes):
        raw = raw.decode(errors="ignore")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return dict(DISABLED)
    if not isinstance(parsed, dict):
        return dict(DISABLED)

    try:
        sample = float(parsed.get("sample", 1.0))
    except (TypeError, ValueError):
        sample = 1.0
    # The Worker clamps too, but a value out of range here would be invisible
    # in the admin panel while silently meaning something else at the edge.
    sample = min(1.0, max(0.0, sample))

    return {"enabled": bool(parsed.get("enabled", False)), "sample": sample}


def mirror_config() -> dict:
    """The current mirror configuration, as served to the Worker.

    Best-effort by contract: any error answers DISABLED rather than raising,
    because /edge-config failing to answer is what stops the Worker mirroring at
    all, and a 500 here would be a worse outcome than an off switch.
    """
    try:
        raw = RedisClient().client.get(MIRROR_KEY)
    except Exception:
        return dict(DISABLED)
    if raw is None:
        return dict(DISABLED)
    return _coerce(raw)


def mirror_state() -> dict:
    """The configuration plus when it expires, for the admin panel.

    Unlike mirror_config() this raises, so the panel can show that Redis is
    unreachable instead of rendering a confident "off" that nobody set.
    """
    client = RedisClient().client
    raw = client.get(MIRROR_KEY)
    if raw is None:
        return {**DISABLED, "expires_at": None}

    state = _coerce(raw)
    ttl = client.ttl(MIRROR_KEY)
    expires_at = None
    if isinstance(ttl, int) and ttl > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    return {**state, "expires_at": expires_at}


def set_mirror(enabled: bool, sample: float = 1.0, ttl_seconds=None) -> None:
    """Persist the switch.

    Raises on Redis failure so the caller can surface it — an admin who clicks
    the toggle and is told nothing must not be left believing it took.

    ``ttl_seconds=None`` means no expiry ("until turned off").
    """
    client = RedisClient().client
    if not enabled:
        client.delete(MIRROR_KEY)
        return

    try:
        sample = min(1.0, max(0.0, float(sample)))
    except (TypeError, ValueError):
        sample = 1.0

    payload = json.dumps({"enabled": True, "sample": sample}, sort_keys=True)
    if ttl_seconds:
        client.setex(MIRROR_KEY, int(ttl_seconds), payload)
    else:
        client.set(MIRROR_KEY, payload)


def edge_payload(mirror: dict) -> dict:
    """The document /edge-config serves.

    The version is derived from the content rather than stored, so no writer can
    forget to bump it — the same reasoning as services/plugin_manifest.

    Note what is *not* here: the destination host. That stays a deploy-time
    wrangler var, so this endpoint can turn mirroring on and off but can never
    aim it somewhere new. It is also why the document is safe to serve
    unauthenticated — it carries no secret and names no host.
    """
    body = {"mirror": {"enabled": bool(mirror.get("enabled", False)),
                       "sample": float(mirror.get("sample", 1.0))}}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return {"version": version, **body}
