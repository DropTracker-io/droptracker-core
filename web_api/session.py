"""Web API v1 session tokens (Task 02, FRONTEND_PLAN.md §7.1 step 4, §13).

Stateless JWT strategy (the plan's recommended approach): the token is signed
with the existing ``JWT_TOKEN_KEY`` (HS256) and carries only ``sub`` (user_id)
plus issued/expiry claims. No per-request DB read for validation.

Revocation (logout / forced sign-out) is handled with a lightweight Redis
deny-list keyed on the token's ``jti``; absence of Redis simply disables
revocation (tokens still expire).
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Optional

import jwt

from utils.redis import redis_client

_SECRET = os.getenv("JWT_TOKEN_KEY") or os.getenv("ENCRYPTION_KEY") or "dev-insecure-web-secret"
_ALG = "HS256"
# Session lifetime. The BFF cookie maxAge is 7d; keep the token comfortably
# longer-lived than a page session but bounded.
_TTL_SECONDS = int(os.getenv("WEB_API_SESSION_TTL", str(7 * 24 * 3600)))

_DENY_PREFIX = "web:denylist:"


def _rc():
    return getattr(redis_client, "client", None)


def mint_session(user_id: int, ttl: Optional[int] = None) -> str:
    """Return a signed session JWT for ``user_id``."""
    now = int(time.time())
    ttl = ttl if ttl is not None else _TTL_SECONDS
    payload = {
        "sub": int(user_id),
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALG)


def verify_session(token: str) -> Optional[dict]:
    """Decode + validate a session token. Returns claims, or None if invalid,
    expired, or revoked."""
    if not token:
        return None
    try:
        claims = jwt.decode(token, _SECRET, algorithms=[_ALG])
    except jwt.PyJWTError:
        return None

    jti = claims.get("jti")
    if jti:
        conn = _rc()
        if conn is not None:
            try:
                if conn.get(f"{_DENY_PREFIX}{jti}"):
                    return None
            except Exception:
                pass
    return claims


def revoke_session(token: str) -> bool:
    """Add a token's ``jti`` to the Redis deny-list until it would expire."""
    try:
        claims = jwt.decode(
            token, _SECRET, algorithms=[_ALG], options={"verify_exp": False}
        )
    except jwt.PyJWTError:
        return False
    jti = claims.get("jti")
    exp = claims.get("exp")
    if not jti:
        return False
    conn = _rc()
    if conn is None:
        return False
    try:
        ttl = max(1, int(exp) - int(time.time())) if exp else _TTL_SECONDS
        conn.setex(f"{_DENY_PREFIX}{jti}", ttl, "1")
        return True
    except Exception:
        return False
