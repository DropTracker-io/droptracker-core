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
    """Map a `period` query param to a Redis **monthly** partition int.

    Used for the per-player monthly total keys (`player:{id}:{YYYYMM}:total_loot`)
    that only exist monthly. Leaderboard sorted-set reads should use
    `resolve_period` (token-based) instead, which supports day/week/all-time.
    """
    if period and re.fullmatch(r"\d{6}", period):
        return int(period)
    return get_current_partition()


def resolve_period(period: Optional[str]) -> str:
    """Normalize a `period` query param to a canonical partition token
    (`YYYYMM` | `YYYYWww` | `YYYYMMDD` | `all`), shared with the write path
    (`utils.partitions`). Unknown forms fall back to the current month."""
    from utils.partitions import resolve_period as _resolve

    return _resolve(period)


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
def leaderboard_key(partition, group_id: Optional[int] = None, npc_id: Optional[int] = None) -> str:
    """Canonical player-board key: ``leaderboard:{token}[:group:{gid}]``.

    ``partition`` may be a monthly int or any partition token string
    (see ``resolve_period``). For NPC boards use ``npc_leaderboard_key`` —
    those live under a distinct, pre-existing scheme (see below).
    """
    key = f"leaderboard:{partition}"
    if group_id:
        key += f":group:{group_id}"
    if npc_id:
        key += f":npc:{npc_id}"
    return key


def npc_leaderboard_key(partition, npc_id: int, group_id: Optional[int] = None) -> str:
    """NPC player-board key in the scheme actually populated today by
    ``player.get_score_at_npc`` / ``services.hall_of_fame``:

        leaderboard:npc:{npcId}:{token}
        leaderboard:group:{groupId}:npc:{npcId}:{token}

    (The §8.5 canonical form nests the token first; unifying the writers is a
    separate bot-side migration. The Web API reads what exists so NPC scopes
    return live data.)
    """
    if group_id:
        return f"leaderboard:group:{group_id}:npc:{npc_id}:{partition}"
    return f"leaderboard:npc:{npc_id}:{partition}"


# Group-total precompute already maintained by the lootboard generator
# (`gleaderboard:{monthly_partition}` — member=group_id, score=total_loot).
def group_totals_key(partition) -> str:
    return f"gleaderboard:{partition}"


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
# Privacy: players excluded from all public surfaces (leaderboards, search,
# profiles, live feed). A player is hidden when its own `players.hidden` flag
# is set (bot `/hideme <account>`, web per-account toggle) or when the owning
# user opted out entirely (`users.hidden`; bot `/hideme all`, web "Hidden").
# --------------------------------------------------------------------------- #
_HIDDEN_TTL = 60.0


def hidden_player_ids() -> set:
    """Set of player ids to omit from public reads. Cached in-process ~60s."""
    cached = cache_get("privacy:hidden_pids", _HIDDEN_TTL)
    if cached is not None:
        return cached
    from sqlalchemy import or_

    from db import Player, User

    out: set = set()
    try:
        with db_session() as s:
            rows = (
                s.query(Player.player_id)
                .outerjoin(User, User.user_id == Player.user_id)
                .filter(or_(Player.hidden.is_(True), User.hidden.is_(True)))
                .all()
            )
            out = {int(pid) for (pid,) in rows}
    except Exception:
        # Fail open: never let the privacy filter take down public reads.
        out = set()
    cache_set("privacy:hidden_pids", out)
    return out


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


class ProblemException(Exception):
    """Raise from anywhere in a request to abort with an RFC-7807 response.

    A single Quart error handler (registered in ``create_app``) converts it to a
    ``problem()`` response, so route/dependency helpers can bail out early
    without threading a response object back up the call stack.
    """

    def __init__(self, status: int, title: str, detail: Optional[str] = None):
        super().__init__(title)
        self.status = status
        self.title = title
        self.detail = detail

    def to_response(self):
        return problem(self.status, self.title, self.detail)


def abort_problem(status: int, title: str, detail: Optional[str] = None):
    raise ProblemException(status, title, detail)


# --------------------------------------------------------------------------- #
# Caching helpers for public reads (§6.5). Authed reads use `no_store`.
# --------------------------------------------------------------------------- #
def with_cache_headers(response, max_age: int = 15, etag_seed: Optional[str] = None):
    """Attach `Cache-Control` + (optional) weak `ETag` to a public read.

    The BFF layers ISR on top of this. `etag_seed` should be a cheap,
    deterministic string for the payload (e.g. the JSON body).
    """
    response.headers["Cache-Control"] = f"public, max-age={max_age}"
    if etag_seed is not None:
        import hashlib

        digest = hashlib.sha1(etag_seed.encode("utf-8", "ignore")).hexdigest()[:16]
        response.headers["ETag"] = f'W/"{digest}"'
    return response


def private_no_store(response):
    response.headers["Cache-Control"] = "private, no-store"
    return response


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
