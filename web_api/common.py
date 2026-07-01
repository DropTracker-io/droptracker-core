"""Shared helpers for the DropTracker Web API v1.

The Web API v1 is a **separate process** (recommended port :31325) that backs the
new Next.js front-end (droptracker-web). It reuses the existing ORM models and
Redis leaderboard data by import, but runs its own event loop and — because it
is a distinct OS process — its own SQLAlchemy connection pool, so website traffic
never competes with the RuneLite intake API on :31323.

This module intentionally avoids importing the Discord bot stack (interactions,
PIL, etc.). It only touches `db` models and the raw Redis client.
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Optional

from quart import jsonify

from db import Session

# Raw Redis client (singleton) — no bot dependencies.
from utils.redis import redis_client


# --------------------------------------------------------------------------- #
# Money envelope + number formatting (mirrors utils.format.format_number so the
# Web API does not need to import the Discord stack).
# --------------------------------------------------------------------------- #
def format_number(number: Any) -> str:
    if not number:
        return "0"
    try:
        number = number.decode("utf-8")  # bytes from Redis
    except Exception:
        pass
    try:
        number = int(float(number))
    except Exception:
        try:
            number = int(number)
        except Exception:
            return "0"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    elif number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    elif number >= 1_000:
        return f"{number / 1_000:.2f}K"
    else:
        return f"{number:,}"


def money(value: Any) -> dict:
    """The `{ value, value_formatted }` envelope used throughout the contract."""
    try:
        v = int(float(value)) if value is not None else 0
    except Exception:
        v = 0
    return {"value": v, "value_formatted": format_number(v)}


# --------------------------------------------------------------------------- #
# Time-partition handling (FRONTEND_PLAN.md §6.5 / Task 07).
#
# Today only monthly (`YYYYMM`) partitions are fully maintained in Redis. Daily/
# weekly/all-time require the write-path work in Task 07 Part A. Until then we
# accept the parameter but resolve unsupported forms to the current month, and
# echo back the resolved partition so the client sees what it actually got.
# --------------------------------------------------------------------------- #
def get_current_partition() -> int:
    now = datetime.now()
    return now.year * 100 + now.month


def period_to_partition(period: Optional[str]) -> int:
    """Map a `period` query param to a Redis monthly partition int.

    `YYYYMM` is honored directly. `all`, `YYYYWW`, and `YYYYMMDD` are not yet
    backed by dedicated leaderboard sorted sets, so they fall back to the
    current month (documented limitation until Task 07 lands).
    """
    if period and re.fullmatch(r"\d{6}", period):
        return int(period)
    return get_current_partition()


# --------------------------------------------------------------------------- #
# DB session context (own pool by virtue of being a separate process).
# --------------------------------------------------------------------------- #
@contextmanager
def db_session():
    s = Session()
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Redis convenience (canonical key scheme, §8.5 / Task 07 Part A).
# --------------------------------------------------------------------------- #
def leaderboard_key(partition: int, group_id: Optional[int] = None, npc_id: Optional[int] = None) -> str:
    key = f"leaderboard:{partition}"
    if group_id:
        key += f":group:{group_id}"
    if npc_id:
        key += f":npc:{npc_id}"
    return key


def _rc():
    return getattr(redis_client, "client", None)


def player_month_total(player_id: int, partition: Optional[int] = None) -> int:
    """Player's monthly total loot from Redis (string key, falling back to the
    global leaderboard score). Mirrors services.redis_updates.get_player_current_month_total."""
    if partition is None:
        partition = get_current_partition()
    conn = _rc()
    if conn is None:
        return 0
    try:
        raw = conn.get(f"player:{player_id}:{partition}:total_loot")
        if raw is None:
            score = conn.zscore(leaderboard_key(partition), player_id)
            return int(float(score)) if score is not None else 0
        return int(float(raw))
    except Exception:
        return 0


def player_list_loot_sum(player_ids: Iterable[int], partition: Optional[int] = None) -> int:
    total = 0
    for pid in player_ids:
        total += player_month_total(pid, partition)
    return total


def player_global_rank(player_id: int, partition: Optional[int] = None) -> Optional[int]:
    """1-based global rank, or None if the player is not on the board."""
    if partition is None:
        partition = get_current_partition()
    conn = _rc()
    if conn is None:
        return None
    try:
        rank = conn.zrevrank(leaderboard_key(partition), player_id)
        return int(rank) + 1 if rank is not None else None
    except Exception:
        return None


def decode_member(raw) -> Optional[int]:
    try:
        s = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return int(s)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Tiny in-process TTL cache (hot public reads). Per-process, so it never leaks
# across the intake API.
# --------------------------------------------------------------------------- #
_cache: dict[str, tuple[float, Any]] = {}


def cache_get(key: str, ttl: float):
    entry = _cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if (time.time() - ts) > ttl:
        _cache.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any):
    _cache[key] = (time.time(), value)


# --------------------------------------------------------------------------- #
# RFC-7807 problem responses (FRONTEND_PLAN.md §6.5 error envelope).
# --------------------------------------------------------------------------- #
def problem(status: int, title: str, detail: Optional[str] = None):
    body = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    resp = jsonify(body)
    resp.status_code = status
    resp.headers["content-type"] = "application/problem+json"
    return resp


def parse_page(request, default_limit: int = 25, max_limit: int = 100) -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        limit = int(request.args.get("limit", default_limit))
    except Exception:
        limit = default_limit
    limit = max(1, min(limit, max_limit))
    return page, limit
