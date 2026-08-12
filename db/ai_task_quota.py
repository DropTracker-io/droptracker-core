"""Daily quota for AI-assisted event task generation.

The generator spawns a headless Claude Code CLI session on the machine's
subscription auth, so a generation costs no metered API spend — the scarce
resource is *machine capacity*, shared with the owner's adminbot tooling and
with no remaining-quota signal available from the CLI. This module is the
guard rail: a per-group daily allowance (the ``ai_task_gen_daily`` integer
entitlement, so superadmins tune it per tier on /admin/tiers with no schema
change) plus a per-user sub-cap so one member cannot burn a whole group's day.

Counters live in Redis on the same ``web:ratelimit:`` convention the site and
submission limiters use: ``INCR`` + ``EXPIRE`` on first hit, i.e. a fixed
window that resets ~24h after a group's first generation of the day rather
than at midnight. That is deliberately simpler than a rolling window — the
numbers are generous enough that the boundary behaviour does not matter, and
it costs one round trip instead of a sorted-set trim.

**Fail-open**: if Redis is unreachable we allow the generation. A convenience
feature must not become unavailable because the counter is down, and the
concurrency cap in the web layer still bounds the blast radius.

It lives in ``db/`` (not ``web_api/``) so worker/bot processes could consult
it without importing the Quart app package — the db.entitlements precedent.
"""
from __future__ import annotations

import os
from typing import Optional

#: Entitlement key holding a tier's daily per-group allowance.
ENTITLEMENT_KEY = "ai_task_gen_daily"

#: Per-user daily sub-cap, applied on top of the group allowance so a single
#: member cannot consume the entire group's day. Env-tunable; 0 disables it.
USER_DAILY_CAP = int(os.getenv("AI_TASK_GEN_USER_DAILY_CAP", "15"))

#: Site-wide daily ceiling — the circuit breaker. Sized well above the sum of
#: realistic group demand; it exists to bound a pathological day, not to shape
#: normal use. 0 disables it.
GLOBAL_DAILY_CAP = int(os.getenv("AI_TASK_GEN_GLOBAL_DAILY_CAP", "2000"))

_WINDOW_SECONDS = 86400


def _rc():
    """Raw redis client, or None when unavailable (callers fail open)."""
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def _group_key(group_id: int) -> str:
    return f"web:ratelimit:aigen:group:{group_id}"


def _user_key(user_id: int) -> str:
    return f"web:ratelimit:aigen:user:{user_id}"


_GLOBAL_KEY = "web:ratelimit:aigen:global"


def _read(conn, key: str) -> int:
    try:
        raw = conn.get(key)
        return int(raw) if raw is not None else 0
    except Exception:
        return 0


def _bump(conn, key: str) -> int:
    """INCR + set the window on first hit. Returns the post-increment count."""
    count = int(conn.incr(key))
    if count == 1:
        conn.expire(key, _WINDOW_SECONDS)
    return count


def group_allowance(s, group_id: Optional[int], *, is_superadmin: bool = False) -> int:
    """The group's configured daily allowance (0 = feature off for the tier).

    Superadmins and global (group-less) events resolve to an effectively
    unbounded allowance — same posture as every other entitlement gate. The
    caller passes ``is_superadmin`` rather than a user object so this module
    keeps using only ``db.entitlements`` and never imports the Quart app.
    """
    from db.entitlements import resolve_group_entitlements

    if is_superadmin or group_id is None:
        return GLOBAL_DAILY_CAP or 1_000_000
    ents = resolve_group_entitlements(s, group_id)
    try:
        return int(ents.get(ENTITLEMENT_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def status(s, group_id: Optional[int], user_id: int, *, is_superadmin: bool = False) -> dict:
    """Read-only quota snapshot for the group+user pair. Never mutates."""
    limit = group_allowance(s, group_id, is_superadmin=is_superadmin)
    conn = _rc()
    used = _read(conn, _group_key(group_id)) if conn is not None and group_id is not None else 0
    user_used = _read(conn, _user_key(user_id)) if conn is not None else 0
    remaining = max(0, limit - used)
    if USER_DAILY_CAP:
        remaining = min(remaining, max(0, USER_DAILY_CAP - user_used))
    return {
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "user_used": user_used,
        "user_limit": USER_DAILY_CAP,
        "allowed": limit > 0 and remaining > 0,
    }


class QuotaExceeded(Exception):
    """Raised by :func:`consume`; ``code`` is machine-readable for the BFF."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def consume(s, group_id: Optional[int], user_id: int, *, is_superadmin: bool = False) -> dict:
    """Charge one generation against the group + user + global counters.

    Raises :class:`QuotaExceeded` when a cap is already reached, without
    charging. Returns the post-charge status dict on success.
    """
    limit = group_allowance(s, group_id, is_superadmin=is_superadmin)
    if limit <= 0:
        raise QuotaExceeded(
            "ai_gen_not_available",
            "AI task generation isn't included on this group's subscription tier.",
        )

    conn = _rc()
    if conn is None:
        # Fail open: no counter available, but the tier does allow the feature.
        return {"limit": limit, "used": 0, "remaining": limit, "allowed": True, "degraded": True}

    try:
        if group_id is not None and _read(conn, _group_key(group_id)) >= limit:
            raise QuotaExceeded(
                "ai_gen_group_quota",
                f"This group has used all {limit} AI task generations for today.",
            )
        if USER_DAILY_CAP and _read(conn, _user_key(user_id)) >= USER_DAILY_CAP:
            raise QuotaExceeded(
                "ai_gen_user_quota",
                f"You've used your {USER_DAILY_CAP} AI task generations for today.",
            )
        if GLOBAL_DAILY_CAP and _read(conn, _GLOBAL_KEY) >= GLOBAL_DAILY_CAP:
            raise QuotaExceeded(
                "ai_gen_busy",
                "AI task generation is temporarily unavailable site-wide. Please try again later.",
            )

        used = _bump(conn, _group_key(group_id)) if group_id is not None else 0
        _bump(conn, _user_key(user_id))
        _bump(conn, _GLOBAL_KEY)
    except QuotaExceeded:
        raise
    except Exception:
        # Redis blew up mid-charge — fail open rather than block the feature.
        return {"limit": limit, "used": 0, "remaining": limit, "allowed": True, "degraded": True}

    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "allowed": limit - used > 0,
    }


def refund(group_id: Optional[int], user_id: int) -> None:
    """Give a charge back when the generation itself failed.

    Best-effort and never raises: a lost refund costs the user one generation,
    while a raised exception here would mask the real generation error.
    """
    conn = _rc()
    if conn is None:
        return
    for key in (
        _group_key(group_id) if group_id is not None else None,
        _user_key(user_id),
        _GLOBAL_KEY,
    ):
        if not key:
            continue
        try:
            if int(conn.get(key) or 0) > 0:
                conn.decr(key)
        except Exception:
            pass
