"""Runtime configuration for the Cloudflare edge Worker (edge/intake-capture).

Today this carries one thing: whether, and for whom, the Worker also mirrors
production submissions at the dev instance. Superadmins set it from the web
admin panel (web_api/routes/admin.py); the Worker learns it by polling
``GET /edge-config`` on the intake API, which reads the state written here.

The mirror has three modes:

* ``off`` — production only.
* ``testers`` — only Bug Testers' submissions. The Worker recognises them by a
  keyed digest of the account hash each submission carries (see
  :func:`tester_digests`). Meant to stay on.
* ``all`` — the firehose: every submission, subject to ``sample``, with
  testers' copies still labelled as theirs. A debugging mode, time-boxed.

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
only consumer is /edge-config, which Cloudflare edge-caches briefly, so the
real query rate is a handful per minute and a cached value would only add
latency to the toggle taking effect.
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

from utils.redis import RedisClient

MIRROR_KEY = "edge:mirror"

MODE_OFF = "off"
MODE_TESTERS = "testers"
MODE_ALL = "all"
MODES = (MODE_OFF, MODE_TESTERS, MODE_ALL)

#: What every failure path resolves to. ``enabled`` means "mirror everyone" —
#: it is what a Worker deployed before modes existed reads, so it must never be
#: true in testers mode.
DISABLED = {"mode": MODE_OFF, "enabled": False, "sample": 1.0}

#: Offered by the admin panel for the firehose. Unbounded is possible but
#: deliberately not a default — mirroring everyone is a debugging mode, and one
#: left on for a week is how the dev box quietly fills its disk. Testers mode is
#: tiny by comparison and defaults to no expiry.
TTL_CHOICES = (3600, 4 * 3600, 24 * 3600)

#: Where the digest list is cached (services.tester_roster clears it on change).
DIGEST_CACHE_KEY = "edge:testers:digests"
DIGEST_CACHE_SECONDS = 300
#: 16 hex characters = 64 bits: no accidental match among a few dozen testers
#: across ~400k submissions a day, and a short list to ship.
DIGEST_CHARS = 16


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

    if "mode" in parsed:
        mode = parsed["mode"] if parsed["mode"] in (MODE_TESTERS, MODE_ALL) else MODE_OFF
    else:
        # Written before modes existed, when "enabled" meant everyone.
        mode = MODE_ALL if bool(parsed.get("enabled", False)) else MODE_OFF
    return {"mode": mode, "enabled": mode == MODE_ALL, "sample": sample}


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


def set_mirror(mode, sample: float = 1.0, ttl_seconds=None) -> None:
    """Persist the switch.

    ``mode`` is one of MODES; a bool is still accepted (True = everyone), as
    the switch was before modes existed. Raises on Redis failure so the caller
    can surface it — an admin who clicks the toggle and is told nothing must
    not be left believing it took. ``ttl_seconds=None`` means no expiry.
    """
    if isinstance(mode, bool):
        mode = MODE_ALL if mode else MODE_OFF
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")

    client = RedisClient().client
    if mode == MODE_OFF:
        client.delete(MIRROR_KEY)
        return

    try:
        sample = min(1.0, max(0.0, float(sample)))
    except (TypeError, ValueError):
        sample = 1.0

    payload = json.dumps(
        {"mode": mode, "enabled": mode == MODE_ALL, "sample": sample}, sort_keys=True
    )
    if ttl_seconds:
        client.setex(MIRROR_KEY, int(ttl_seconds), payload)
    else:
        client.set(MIRROR_KEY, payload)


# --------------------------------------------------------------------------- #
# Tester digests
# --------------------------------------------------------------------------- #
def tester_key():
    """``EDGE_TESTER_KEY`` as bytes, or None. The Worker holds the same secret."""
    value = (os.getenv("EDGE_TESTER_KEY") or "").strip().strip('"').strip("'")
    return value.encode("utf-8") if value else None


def digest(account_hash: str, key: bytes) -> str:
    """The digest the Worker computes for a submission's ``acc_hash`` field."""
    message = str(account_hash).strip().encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:DIGEST_CHARS]


def _key_tag(key: bytes) -> str:
    """Identifies the key a cached list was built with, without revealing it."""
    return hashlib.sha256(b"edge-tester-key:" + key).hexdigest()[:12]


def tester_digests() -> list:
    """Keyed digests of every tester's account hashes, for the Worker.

    Why digests rather than the hashes: /edge-config is public and
    Cloudflare-cached by design, and the account hash is what the plugin
    endpoints identify a player by. A digest is useless without the key. (An
    auth header would not help — both the Worker's fetch and the origin's
    ``Cache-Control: public`` cache by URL.)

    Empty when no key is configured or the roster cannot be read, which the
    Worker treats as "nobody is a tester": fail closed, like everything here.
    """
    key = tester_key()
    if not key:
        return []
    tag = _key_tag(key)

    client = None
    try:
        client = RedisClient().client
        cached = client.get(DIGEST_CACHE_KEY) if client is not None else None
        if cached is not None:
            parsed = json.loads(cached.decode() if isinstance(cached, bytes) else cached)
            if isinstance(parsed, dict) and parsed.get("k") == tag and isinstance(parsed.get("d"), list):
                return [str(d) for d in parsed["d"]]
    except Exception:
        pass

    try:
        from services.tester_roster import current_account_hashes

        digests = sorted({digest(h, key) for h in current_account_hashes()})
    except Exception as exc:
        print(f"[edge-config] could not read the tester roster: {exc}")
        return []

    if client is not None:
        try:
            client.setex(DIGEST_CACHE_KEY, DIGEST_CACHE_SECONDS,
                         json.dumps({"k": tag, "d": digests}))
        except Exception:
            pass
    return digests


def tester_summary():
    """``{"users": n, "accounts": m, "key_configured": bool}`` for the admin panel, or None."""
    try:
        from db.models import Session
        from services.tester_roster import account_hashes, load_roster

        with Session() as session:
            roster = load_roster(session)
            session.rollback()
    except Exception:
        return None
    return {
        "users": len(roster.get("users") or ()),
        "accounts": len(account_hashes(roster)),
        "key_configured": tester_key() is not None,
    }


def edge_payload(mirror: dict, testers=None) -> dict:
    """The document /edge-config serves.

    The version is derived from the content rather than stored, so no writer can
    forget to bump it — the same reasoning as services/plugin_manifest. Tester
    digests are part of the content, so adding a tester changes the version.

    Note what is *not* here: the destination host. That stays a deploy-time
    wrangler var, so this endpoint can turn mirroring on and off but can never
    aim it somewhere new. It is also why the document is safe to serve
    unauthenticated — it carries no secret and names no host.
    """
    mode = mirror.get("mode")
    if mode not in MODES:
        mode = MODE_ALL if mirror.get("enabled") else MODE_OFF
    body = {"mirror": {
        "mode": mode,
        "enabled": mode == MODE_ALL,
        "sample": float(mirror.get("sample", 1.0)),
    }}
    if mode != MODE_OFF:
        body["mirror"]["testers"] = sorted(str(t) for t in (testers or ()))
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return {"version": version, **body}
