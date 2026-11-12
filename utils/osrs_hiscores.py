"""The official Old School hiscores: parsing, one fetch, and the gate that
keeps us polite.

This is where Wise Old Man gets its numbers too. We read the same public
endpoint, directly, for players the plugin cannot tell us about:

    https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player=NAME

It answers with every skill (rank, level, xp) and every activity and boss
(rank, score). A value of -1 means the player is not ranked for that entry,
which is "unknown" here and never zero. A 404 means the name is not on that
board at all: renamed, banned, or never ranked.

Rules this module enforces, because Jagex owes us nothing:

- **One global pace.** Every process shares one Redis gate, so the whole
  platform makes at most one request per ``HISCORES_INTERVAL_MS``.
- **Back off when told.** A 429, or a run of errors, opens a breaker and
  nothing is sent until it closes. Hiscores outages are routine on update
  Wednesdays; the plugin's data is unaffected by them.
- **A daily cap**, counted in Redis, so a bug cannot become a flood.
- **An honest User-Agent, no proxies.** If Jagex want us to stop they can
  tell who to ask.

Nothing here decides *who* to look up; that is ``services/hiscores_poller``.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote

BASE_URL = "https://secure.runescape.com"
# Account type (utils.account_types wire string) -> hiscores board. The main
# board lists every account; the ironman boards list the same XP with their
# own ranks and a lower entry bar. Group ironmen have no per-player board.
BOARDS = {
    "normal": "hiscore_oldschool",
    "ironman": "hiscore_oldschool_ironman",
    "hardcore_ironman": "hiscore_oldschool_hardcore_ironman",
    "ultimate_ironman": "hiscore_oldschool_ultimate",
}
MAIN_BOARD = BOARDS["normal"]

USER_AGENT = os.getenv("HISCORES_USER_AGENT", "DropTracker (https://www.droptracker.io)")
INTERVAL_MS = int(os.getenv("HISCORES_INTERVAL_MS", "2000"))
DAILY_CAP = int(os.getenv("HISCORES_DAILY_CAP", "20000"))
REQUEST_TIMEOUT = float(os.getenv("HISCORES_TIMEOUT", "15"))
# Consecutive failures before the breaker opens, and how long it stays open.
BREAKER_FAILURES = 5
BREAKER_SECONDS = 600
RATE_LIMIT_SECONDS = 900

GATE_KEY = "hiscores:gate"
BREAKER_KEY = "hiscores:breaker"
FAILURES_KEY = "hiscores:failures"
COUNT_KEY = "hiscores:count:{day}"

OK, NOT_FOUND, RATE_LIMITED, ERROR, BLOCKED = "ok", "not_found", "rate_limited", "error", "blocked"

# Hiscores names that do not normalise to the metric slug Wise Old Man uses.
# Our consumers were written against those slugs, so we keep them.
_SLUG_OVERRIDES = {
    "tombs_of_amascut_expert_mode": "tombs_of_amascut_expert",
    "lms_rank": "last_man_standing",
    "pvp_arena_rank": "pvp_arena",
    "rifts_closed": "guardians_of_the_rift",
}
OVERALL = "overall"


def board_for(account_type: Optional[str]) -> str:
    """The board to ask first for an account of this type."""
    return BOARDS.get(account_type or "", MAIN_BOARD)


def metric_slug(name: Any) -> str:
    """``'Theatre of Blood: Hard Mode'`` -> ``'theatre_of_blood_hard_mode'``."""
    token = str(name or "").strip().lower()
    for ch in ("'", ":", ",", ".", "(", ")"):
        token = token.replace(ch, "")
    for ch in (" ", "-"):
        token = token.replace(ch, "_")
    while "__" in token:
        token = token.replace("__", "_")
    token = token.strip("_")
    return _SLUG_OVERRIDES.get(token, token)


@dataclass
class Hiscores:
    """One player's hiscores entry, with unranked values left out."""

    skills: Dict[str, int] = field(default_factory=dict)       # skill -> xp
    activities: Dict[str, int] = field(default_factory=dict)   # metric -> score
    ranks: Dict[str, int] = field(default_factory=dict)        # metric -> rank
    total_xp: Optional[int] = None
    total_level: Optional[int] = None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse(payload: Any) -> Optional[Hiscores]:
    """Read an ``index_lite.json`` body. None if it is not one.

    Entries are matched by name, not position: Jagex add skills and bosses
    in the middle of the list.
    """
    if not isinstance(payload, dict):
        return None
    skills, activities = payload.get("skills"), payload.get("activities")
    if not isinstance(skills, list) or not isinstance(activities, list):
        return None
    out = Hiscores()
    for entry in skills:
        if not isinstance(entry, dict):
            continue
        slug = metric_slug(entry.get("name"))
        xp, rank, level = _int(entry.get("xp")), _int(entry.get("rank")), _int(entry.get("level"))
        if not slug or xp is None or xp < 0:
            continue
        if slug == OVERALL:
            out.total_xp, out.total_level = xp, level
        else:
            out.skills[slug] = xp
        if rank is not None and rank > 0:
            out.ranks[slug] = rank
    for entry in activities:
        if not isinstance(entry, dict):
            continue
        slug = metric_slug(entry.get("name"))
        score, rank = _int(entry.get("score")), _int(entry.get("rank"))
        if not slug or score is None or score <= 0:
            continue
        out.activities[slug] = score
        if rank is not None and rank > 0:
            out.ranks[slug] = rank
    if not out.skills and out.total_xp is None:
        return None
    return out


def url_for(player_name: str, board: str = MAIN_BOARD) -> str:
    return f"{BASE_URL}/m={board}/index_lite.json?player={quote(str(player_name).strip())}"


# --------------------------------------------------------------------------- #
# The gate: pace, breaker, daily cap. All state is in Redis so every process
# shares it. A Redis error fails CLOSED: no state, no request.
# --------------------------------------------------------------------------- #

def _count_key(now: Optional[datetime] = None) -> str:
    return COUNT_KEY.format(day=(now or datetime.utcnow()).strftime("%Y%m%d"))


def requests_today(redis_conn, now: Optional[datetime] = None) -> int:
    try:
        return int(redis_conn.get(_count_key(now)) or 0)
    except Exception:
        return DAILY_CAP


def breaker_open(redis_conn) -> bool:
    try:
        return bool(redis_conn.exists(BREAKER_KEY))
    except Exception:
        return True


def may_request(redis_conn, now: Optional[datetime] = None) -> Optional[str]:
    """Why a request may NOT be made right now, or None if it may."""
    try:
        from utils.external_calls import external_apis_allowed

        if not external_apis_allowed():
            return "dev instance"
    except Exception:
        return "dev guard unavailable"
    if breaker_open(redis_conn):
        return "breaker open"
    if requests_today(redis_conn, now) >= DAILY_CAP:
        return "daily cap reached"
    return None


def take_slot(redis_conn) -> bool:
    """Claim the one request slot of this interval. False if it is taken."""
    try:
        return bool(redis_conn.set(GATE_KEY, "1", nx=True, px=INTERVAL_MS))
    except Exception:
        return False


async def wait_for_slot(redis_conn, deadline: float) -> bool:
    """Wait (until the ``time.monotonic()`` deadline) for a request slot."""
    while time.monotonic() < deadline:
        if take_slot(redis_conn):
            return True
        await asyncio.sleep(min(0.25, INTERVAL_MS / 1000.0))
    return False


def record_request(redis_conn, now: Optional[datetime] = None) -> None:
    try:
        key = _count_key(now)
        redis_conn.incr(key)
        redis_conn.expire(key, 14 * 86400)
    except Exception:
        pass


def record_outcome(redis_conn, status: str, retry_after: Optional[float] = None) -> None:
    """Feed the breaker. A 404 is an answer, not a failure."""
    try:
        if status in (OK, NOT_FOUND):
            redis_conn.delete(FAILURES_KEY)
        elif status == RATE_LIMITED:
            redis_conn.set(BREAKER_KEY, "rate limited",
                           ex=int(max(RATE_LIMIT_SECONDS, retry_after or 0)))
        elif status == ERROR:
            failures = int(redis_conn.incr(FAILURES_KEY))
            redis_conn.expire(FAILURES_KEY, BREAKER_SECONDS)
            if failures >= BREAKER_FAILURES:
                redis_conn.set(BREAKER_KEY, f"{failures} failures", ex=BREAKER_SECONDS)
                redis_conn.delete(FAILURES_KEY)
    except Exception:
        pass


@dataclass
class FetchResult:
    status: str
    hiscores: Optional[Hiscores] = None
    board: str = MAIN_BOARD
    detail: str = ""


def _retry_after(headers) -> Optional[float]:
    try:
        return float(headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None


async def fetch(http, redis_conn, player_name: str, board: str = MAIN_BOARD, *,
                wait_seconds: float = 30.0) -> FetchResult:
    """One gated request. ``http`` is an ``aiohttp.ClientSession``.

    Returns BLOCKED without touching the network when the gate says no.
    """
    reason = may_request(redis_conn)
    if reason:
        return FetchResult(BLOCKED, board=board, detail=reason)
    if not await wait_for_slot(redis_conn, time.monotonic() + wait_seconds):
        return FetchResult(BLOCKED, board=board, detail="no slot")
    record_request(redis_conn)
    try:
        import aiohttp

        async with http.get(url_for(player_name, board), headers={"User-Agent": USER_AGENT},
                            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
            if resp.status == 404:
                result = FetchResult(NOT_FOUND, board=board)
            elif resp.status == 429:
                record_outcome(redis_conn, RATE_LIMITED, _retry_after(resp.headers))
                return FetchResult(RATE_LIMITED, board=board, detail="429")
            elif resp.status != 200:
                result = FetchResult(ERROR, board=board, detail=f"http {resp.status}")
            else:
                # During an outage Jagex answer 200 with an HTML error page.
                parsed = parse(await resp.json(content_type=None))
                result = (FetchResult(OK, parsed, board) if parsed is not None
                          else FetchResult(ERROR, board=board, detail="unreadable body"))
    except Exception as exc:
        result = FetchResult(ERROR, board=board, detail=type(exc).__name__)
    record_outcome(redis_conn, result.status)
    return result
