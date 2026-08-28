"""Data API (v2) key logic: mint, parse, verify, effective limits.

Lives in ``db/`` (not ``data_api/``) for the same reason as ``entitlements``
and ``event_rate_limits``: the web API mints keys, the data API verifies them,
and admin tooling lists them — none of which should import each other's app
package. Everything here is a pure function over rows/values; the ORM classes
are lazy-imported so the module loads standalone under the test bootstrap's
``db`` stub (the ``db.entitlements`` load-by-path pattern).

Token shape::

    dtk_<key_id>_<secret>

The row id rides in the token so authentication is a primary-key fetch plus a
constant-time digest comparison. The secret alone is never enough (no scan by
hash), and the id alone reveals nothing (401 for a bad secret is
indistinguishable from 401 for a missing row — see :func:`verify_key`).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets as _secrets
from datetime import datetime
from typing import Optional, Tuple

TOKEN_PREFIX = "dtk"
#: What a key may read. 'global' is every group and every player — for
#: third-party integrations, staff-issued only. It is NOT a visibility
#: override: hidden players stay hidden to every scope.
SCOPES = ("user", "group", "global")
#: Secret half: 48 hex chars = 24 random bytes.
SECRET_HEX_CHARS = 48
DEFAULT_TIER = "standard"

_TOKEN_RE = re.compile(r"^dtk_(\d{1,18})_([0-9a-f]{16,64})$")

#: Limit fields resolved tier-first with per-key override.
LIMIT_FIELDS = (
    "requests_per_min",
    "cost_units_per_min",
    "requests_per_day",
    "max_concurrency",
)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint_token(key_id: int) -> Tuple[str, str, str]:
    """``(token, token_hash, token_prefix)`` for a freshly created row.

    The caller inserts the row first (to obtain ``key_id``), then updates it
    with the hash/prefix, and shows ``token`` exactly once.
    """
    secret = _secrets.token_hex(SECRET_HEX_CHARS // 2)
    return (
        f"{TOKEN_PREFIX}_{key_id}_{secret}",
        hash_secret(secret),
        secret[:8],
    )


def parse_token(token: str) -> Optional[Tuple[int, str]]:
    """``(key_id, secret)``, or None for anything that is not token-shaped."""
    if not isinstance(token, str):
        return None
    match = _TOKEN_RE.match(token.strip())
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def verify_key(row, secret: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """``(ok, reason)`` — whether ``secret`` authenticates ``row`` right now.

    ``row`` may be an ORM ApiKey or anything with the same attributes; ``None``
    (no such id) is accepted so callers can funnel every failure through one
    path and return an identical 401 for missing-row and wrong-secret — the
    token's embedded id must not become an existence oracle.

    Reasons are for logs/metrics only, never for response bodies.
    """
    if row is None:
        return False, "unknown"
    if not hmac.compare_digest(getattr(row, "token_hash", "") or "", hash_secret(secret)):
        return False, "bad_secret"
    now = now or datetime.utcnow()
    if getattr(row, "revoked_at", None) is not None:
        return False, "revoked"
    expires_at = getattr(row, "expires_at", None)
    if expires_at is not None and expires_at <= now:
        return False, "expired"
    return True, "ok"


def effective_limits(row, tier_row) -> dict:
    """The limits this key actually gets: per-key override, else tier value.

    ``tier_row`` may be ``None`` (tier deleted out from under the key); the
    fallbacks are deliberately the floor, not a failure — a dangling tier must
    never grant unlimited access.
    """
    # Half the entry tier: a key whose tier row vanished still works, but on
    # noticeably less than the cheapest real tier grants.
    fallback = {
        "requests_per_min": 30,
        "cost_units_per_min": 75_000,
        "requests_per_day": 5_000,
        "max_concurrency": 2,
    }
    limits = {}
    for field in LIMIT_FIELDS:
        override = getattr(row, field, None) if row is not None else None
        if override is not None:
            limits[field] = int(override)
            continue
        tier_value = getattr(tier_row, field, None) if tier_row is not None else None
        limits[field] = int(tier_value) if tier_value is not None else fallback[field]
    return limits


def key_descriptor(row, tier_row) -> dict:
    """The request-context dict the data API carries around per request."""
    owner_user_id = getattr(row, "owner_user_id", None)
    group_id = getattr(row, "group_id", None)
    # Read the stored scope, but never *infer* 'global' from absent owners:
    # a row that somehow has neither is a broken row, and the safe reading of
    # a broken row is the narrowest scope, not the widest.
    scope = getattr(row, "scope", None)
    if scope not in SCOPES:
        scope = "user" if owner_user_id is not None else "group"
    return {
        "key_id": int(row.id),
        "label": getattr(row, "label", "") or "",
        "tier": getattr(row, "tier_key", None) or DEFAULT_TIER,
        "scope": scope,
        "owner_type": scope,
        "owner_user_id": owner_user_id,
        "group_id": group_id,
        "limits": effective_limits(row, tier_row),
    }


# ── DB glue (lazy ORM imports; callers pass a live session) ──────────────────

def load_key(session, key_id: int):
    """``(ApiKey row or None, ApiKeyTier row or None)`` for one id."""
    from db.models import ApiKey, ApiKeyTier

    row = session.query(ApiKey).filter(ApiKey.id == key_id).first()
    if row is None:
        return None, None
    tier = (
        session.query(ApiKeyTier)
        .filter(ApiKeyTier.tier_key == row.tier_key)
        .first()
    )
    return row, tier


def create_key(
    session,
    *,
    owner_user_id: Optional[int] = None,
    group_id: Optional[int] = None,
    scope: Optional[str] = None,
    label: str = "",
    tier_key: str = DEFAULT_TIER,
    created_by_user_id: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> Tuple[object, str]:
    """Insert a key and return ``(row, plaintext_token)``. Caller commits.

    ``scope`` defaults to whichever owner was supplied. **A global key must
    ask for one**: ``scope="global"`` with no owner. Omitting both owners
    without saying so is an error, not an implicit grant of everything — the
    table CHECK enforces the same rule, but failing here gives the caller a
    ValueError instead of an IntegrityError from deep inside a route.
    """
    if scope is None:
        scope = "user" if owner_user_id is not None else "group"
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")

    if scope == "global":
        if owner_user_id is not None or group_id is not None:
            raise ValueError("a global key has no owner")
    elif scope == "user":
        if owner_user_id is None or group_id is not None:
            raise ValueError("a user key needs owner_user_id and no group_id")
    else:
        if group_id is None or owner_user_id is not None:
            raise ValueError("a group key needs group_id and no owner_user_id")

    from db.models import ApiKey

    row = ApiKey(
        token_hash="",  # placeholder until the id exists
        token_prefix="",
        label=label[:64],
        scope=scope,
        owner_user_id=owner_user_id,
        group_id=group_id,
        tier_key=tier_key,
        created_by_user_id=created_by_user_id,
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()  # assigns row.id
    token, token_hash, token_prefix = mint_token(row.id)
    row.token_hash = token_hash
    row.token_prefix = token_prefix
    return row, token
