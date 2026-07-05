"""Canonical time-partition tokens for leaderboards (FRONTEND_PLAN.md §6.5, §8.5).

A *partition token* is the string embedded in a Redis leaderboard key after the
``leaderboard:`` prefix. Both the write path (``services/redis_updates.py``) and
the Web API read path (``web_api``) compute tokens with these helpers so the two
never drift.

Tokens (matching the contract's ``PeriodSchema``):
    monthly   "YYYYMM"        e.g. "202606"   (== the legacy int partition)
    weekly    "YYYYWww"       e.g. "2026W27"  (ISO year + ISO week)
    daily     "YYYYMMDD"      e.g. "20260617"
    all-time  "all"

The monthly token equals ``str(year*100+month)``, so ``leaderboard:{token}`` is
byte-identical to the pre-existing ``leaderboard:{int_partition}`` key — the
monthly board is unchanged.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

ALL = "all"

_MONTH_RE = re.compile(r"^\d{6}$")
_WEEK_RE = re.compile(r"^\d{4}W\d{2}$", re.IGNORECASE)
_DAY_RE = re.compile(r"^\d{8}$")


def month_token(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now()
    return str(dt.year * 100 + dt.month)


def week_token(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now()
    iso = dt.isocalendar()
    # isocalendar() -> (ISO year, ISO week, ISO weekday); index for 3.8 compat.
    return f"{iso[0]}W{int(iso[1]):02d}"


def day_token(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now()
    return dt.strftime("%Y%m%d")


def all_tokens(dt: Optional[datetime] = None) -> List[str]:
    """Every partition token a drop at ``dt`` contributes to."""
    dt = dt or datetime.now()
    return [month_token(dt), week_token(dt), day_token(dt), ALL]


def is_valid_token(period: Optional[str]) -> bool:
    if not period:
        return False
    if period == ALL:
        return True
    return bool(_MONTH_RE.match(period) or _WEEK_RE.match(period) or _DAY_RE.match(period))


def resolve_period(period: Optional[str]) -> str:
    """Normalize a requested ``period`` query param to a canonical token.

    Recognized forms (``YYYYMM`` | ``YYYYWww`` | ``YYYYMMDD`` | ``all``) pass
    through. The relative sentinels ``day`` / ``week`` / ``month`` resolve to
    the current server-side token — clients should prefer these over computing
    tokens themselves so the week/day arithmetic can never drift from the write
    path. Anything else falls back to the current month.
    """
    if period:
        p = period.strip()
        if p == ALL or _MONTH_RE.match(p) or _DAY_RE.match(p):
            return p
        if _WEEK_RE.match(p):
            return p.upper()
        low = p.lower()
        if low == "day":
            return day_token()
        if low == "week":
            return week_token()
        if low == "month":
            return month_token()
    return month_token()
