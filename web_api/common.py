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
from sqlalchemy import text

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


def player_month_totals(player_ids: Iterable[int],
                        partition: Optional[int] = None) -> dict:
    """Batched :func:`player_month_total`: ``{player_id: gp}`` for many players in
    two Redis round-trips (one pipeline of per-player GETs, one pipeline of
    leaderboard ZSCOREs for the keys that were missing) instead of 2·N.

    Same source and fallback as the single-player version, so the numbers match
    the leaderboard exactly. Any id that can't be resolved maps to ``0``. Ideal
    for enriching a whole sign-up pool at once without N sequential lookups.
    """
    ids = [int(p) for p in player_ids]
    if not ids:
        return {}
    if partition is None:
        partition = get_current_partition()
    conn = _rc()
    if conn is None:
        return {pid: 0 for pid in ids}

    out: dict = {}
    missing: list = []
    try:
        pipe = conn.pipeline()
        for pid in ids:
            pipe.get(f"player:{pid}:{partition}:total_loot")
        raw_vals = pipe.execute()
    except Exception:
        return {pid: 0 for pid in ids}
    for pid, raw in zip(ids, raw_vals):
        if raw is None:
            missing.append(pid)
            continue
        try:
            out[pid] = int(float(raw))
        except Exception:  # noqa: BLE001 — match single-getter's blanket guard
            out[pid] = 0

    if missing:
        try:
            key = leaderboard_key(partition)
            pipe = conn.pipeline()
            for pid in missing:
                pipe.zscore(key, pid)
            scores = pipe.execute()
        except Exception:
            scores = [None] * len(missing)
        for pid, score in zip(missing, scores):
            try:
                out[pid] = int(float(score)) if score is not None else 0
            except Exception:  # noqa: BLE001 — match single-getter's blanket guard
                out[pid] = 0
    return out


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


def cache_delete(key: str) -> None:
    """Evict one entry early — for writes that invalidate a cached derivation
    (e.g. a group rename changing ``canonslug:group:{id}``)."""
    _cache.pop(key, None)


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


def group_ignored_player_ids(group_id) -> set:
    """Set of player ids one group's leaders hid from that group's surfaces.

    The second, group-scoped hiding layer: leaders toggle it on the admin member
    listing (PATCH /api/v1/groups/{id}/hidden-players), which writes an
    ``ignored_players`` row. Unlike ``hidden_player_ids`` above this is NOT
    global — the player stays fully visible everywhere else, including in other
    groups' surfaces — so callers must only apply it to that same group's reads.

    Cached per group ~60s and fails open, matching ``hidden_player_ids``: a
    transient DB fault must not take down the reads it filters.
    """
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return set()
    if gid <= 0:
        return set()

    key = f"privacy:ignored_pids:{gid}"
    cached = cache_get(key, _HIDDEN_TTL)
    if cached is not None:
        return cached

    from db import IgnoredPlayer

    out: set = set()
    try:
        with db_session() as s:
            rows = (
                s.query(IgnoredPlayer.player_id)
                .filter(IgnoredPlayer.group_id == gid)
                .all()
            )
            out = {int(pid) for (pid,) in rows}
    except Exception:
        out = set()
    cache_set(key, out)
    return out


# --------------------------------------------------------------------------- #
# Slugs / "nice URLs" (web_api/routes/resolve.py + `canonical_slug` on the
# group/player/npc/item detail payloads).
#
# Slugs are computed on the fly from the current name — there is no slug column,
# nothing to backfill, and a rename is followed automatically. The SQL
# expression here MUST stay equivalent to the front-end `slugify()` in
# apps/web/lib/slug.ts, so a slug authored on one side resolves on the other.
# --------------------------------------------------------------------------- #
_SLUG_NONALNUM = re.compile(r"[^a-z0-9]+")


def slugify(name: Optional[str]) -> str:
    """lowercase → non-alphanumeric runs to '-' → trim leading/trailing '-'."""
    if not name:
        return ""
    return _SLUG_NONALNUM.sub("-", str(name).lower()).strip("-")


def slug_sql_expr(column: str) -> str:
    """SQL that computes ``slugify(column)`` (MariaDB / MySQL 8 REGEXP_REPLACE).

    ``column`` is interpolated verbatim — pass a trusted column reference only,
    never user input.
    """
    return f"TRIM(BOTH '-' FROM REGEXP_REPLACE(LOWER({column}), '[^a-z0-9]+', '-'))"


_CANON_SLUG_TTL = 300.0


def canonical_slug_for(s, kind: str, entity_id: int, name: Optional[str]) -> Optional[str]:
    """The pretty-URL slug this entity should declare as canonical, or None.

    * npc / item — duplicate names collapse to one primary entity, so the slug
      always belongs to the name; return it (empty name → None).
    * group / player — only when no *other* visible entity shares the slug. A
      colliding name has no unique pretty URL and keeps its id URL as canonical.

    Cached per (kind, id) for a few minutes so the peer-count scan is rare.
    """
    slug = slugify(name)
    if not slug:
        return None
    if kind in ("npc", "item"):
        return slug

    cache_key = f"canonslug:{kind}:{entity_id}"
    cached = cache_get(cache_key, _CANON_SLUG_TTL)
    if cached is not None:
        return cached or None  # "" is the cached form of None

    try:
        if kind == "group":
            expr = slug_sql_expr("group_name")
            row = s.execute(
                text(
                    f"SELECT COUNT(*) FROM groups "
                    f"WHERE group_id > 2 AND group_id <> :id AND {expr} = :slug"
                ),
                {"id": entity_id, "slug": slug},
            ).fetchone()
        else:  # player — ignore hidden players / players of hidden users
            expr = slug_sql_expr("p.player_name")
            row = s.execute(
                text(
                    f"SELECT COUNT(*) FROM players p "
                    f"LEFT JOIN users u ON u.user_id = p.user_id "
                    f"WHERE p.player_id <> :id AND p.hidden IS NOT TRUE "
                    f"AND u.hidden IS NOT TRUE AND {expr} = :slug"
                ),
                {"id": entity_id, "slug": slug},
            ).fetchone()
        unique = int(row[0] or 0) == 0
    except Exception:
        unique = False  # fail closed: fall back to the id url

    result = slug if unique else None
    cache_set(cache_key, result or "")
    return result


# --------------------------------------------------------------------------- #
# RFC-7807 problem responses (FRONTEND_PLAN.md §6.5 error envelope).
# --------------------------------------------------------------------------- #
def problem(
    status: int,
    title: str,
    detail: Optional[str] = None,
    *,
    type_: str = "about:blank",
    extra: Optional[dict] = None,
):
    body = {"type": type_, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if extra:
        # Machine-readable members (RFC-7807 allows extension members) — e.g.
        # the buy-in confirm-on-disable 409 carries {count, total} so the
        # frontend can render the confirm dialog without a second read.
        body.update(extra)
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

    def __init__(
        self,
        status: int,
        title: str,
        detail: Optional[str] = None,
        *,
        type_: str = "about:blank",
        extra: Optional[dict] = None,
    ):
        super().__init__(title)
        self.status = status
        self.title = title
        self.detail = detail
        self.type_ = type_
        self.extra = extra

    def to_response(self):
        return problem(self.status, self.title, self.detail, type_=self.type_, extra=self.extra)


def abort_problem(
    status: int,
    title: str,
    detail: Optional[str] = None,
    *,
    type_: str = "about:blank",
    extra: Optional[dict] = None,
):
    raise ProblemException(status, title, detail, type_=type_, extra=extra)


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


def score_num(value):
    """Team scores JSON-serialize as ints until decimals actually appear
    (loot_sweep awards 2-decimal points; every other kind stays integral) —
    so bingo standings don't suddenly render "120.0"."""
    f = round(float(value or 0), 2)
    return int(f) if f == int(f) else f


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
