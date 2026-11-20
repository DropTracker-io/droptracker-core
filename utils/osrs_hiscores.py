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

- **One global pace, which adjusts itself.** Every process shares one Redis
  gate, so the whole platform starts at most one request per interval (a
  slow answer may overlap the next request; no more than
  ``HISCORES_CONCURRENCY`` are ever in flight). The interval starts at ``HISCORES_INTERVAL_MS`` and is tuned from
  what the hiscores do (`tune_pace`): a little faster each hour that almost
  nothing fails, down to ``HISCORES_MIN_INTERVAL_MS``; much slower at once
  when we are refused, and slower when errors climb. Jagex publish no limit,
  so the limit is found by listening rather than assumed.
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

# Says who we are, what this is and where to reach us, so that anyone at
# Jagex looking at their logs can ask us to slow down instead of blocking us.
# Set HISCORES_USER_AGENT to add a contact address.
USER_AGENT = os.getenv(
    "HISCORES_USER_AGENT",
    "DropTracker-HiscoresPoller/1.0 (+https://www.droptracker.io; OSRS clan stats tracker; "
    "at most two requests a second, backs off on errors)")
# Where the pace starts, and the fastest and slowest it may tune itself to.
INTERVAL_MS = int(os.getenv("HISCORES_INTERVAL_MS", "2000"))
# The floor is half the pace Wise Old Man's own code allows itself: its
# update-player job is limited to one start per 250 ms with 8 running at once
# (server/src/jobs/handlers/update-player.job.ts, read 2026-11-16), straight
# to the same endpoint with no proxy in the repository. We are not WOM, and
# Jagex may know them, so we stop at two a second and still listen.
MIN_INTERVAL_MS = int(os.getenv("HISCORES_MIN_INTERVAL_MS", "500"))
MAX_INTERVAL_MS = 15000
DAILY_CAP = int(os.getenv("HISCORES_DAILY_CAP", "150000"))
REQUEST_TIMEOUT = float(os.getenv("HISCORES_TIMEOUT", "15"))
# Consecutive failures before the breaker opens, and how long it stays open.
BREAKER_FAILURES = 5
BREAKER_SECONDS = 600
RATE_LIMIT_SECONDS = 900

GATE_KEY = "hiscores:gate"
INTERVAL_KEY = "hiscores:interval_ms"      # the tuned interval, shared by every process
WINDOW_KEY = "hiscores:window"             # outcome counts since the pace was last tuned
PACE_LOG_KEY = "hiscores:pace_log"         # recent pace changes, newest first
BREAKER_KEY = "hiscores:breaker"
FAILURES_KEY = "hiscores:failures"
COUNT_KEY = "hiscores:count:{day}"

HEALTH_KEY = "hiscores:health"            # hash: what the health check reads
HOURLY_KEY = "hiscores:hour:{hour}"       # hash of outcome counts for one UTC hour

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


def total_xp(reading: Hiscores) -> int:
    """Overall XP, or the sum of the ranked skills when Overall is unranked."""
    return reading.total_xp if reading.total_xp is not None else sum(reading.skills.values())


async def detect_account_type(fetch_board, main: Hiscores) -> Optional[str]:
    """Work out an account's type from which boards it is on, for a player
    the plugin has never told us about. Returns a `utils.account_types` wire
    string, or None when a board could not be read (try again later).

    ``fetch_board(board)`` is an awaitable returning a `FetchResult`; ``main``
    is the reading already taken from the main board. The rules are Wise Old
    Man's (AssertPlayerTypeService): every account is on the main board; an
    ironman is also on the ironman board with the same XP. More XP on the
    main board than the ironman one means the account de-ironed. A hardcore
    or ultimate is on that board too, with as much XP as its ironman entry;
    a hardcore who died stops gaining there and is a plain ironman.

    Costs one extra request for a regular account and up to three for an
    ironman. Group ironmen have no board of their own and read as normal,
    which is also how they are scored.
    """
    iron = await fetch_board(BOARDS["ironman"])
    if iron.status == NOT_FOUND:
        return "normal"
    if iron.status != OK:
        return None
    iron_xp = total_xp(iron.hiscores)
    if iron_xp < total_xp(main):
        return "normal"
    hardcore = await fetch_board(BOARDS["hardcore_ironman"])
    if hardcore.status == OK and total_xp(hardcore.hiscores) >= iron_xp:
        return "hardcore_ironman"
    if hardcore.status not in (OK, NOT_FOUND):
        return None
    ultimate = await fetch_board(BOARDS["ultimate_ironman"])
    if ultimate.status == OK and total_xp(ultimate.hiscores) >= iron_xp:
        return "ultimate_ironman"
    if ultimate.status not in (OK, NOT_FOUND):
        return None
    return "ironman"


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


def current_interval(redis_conn) -> int:
    """Milliseconds between requests right now: the tuned value if there is
    one, else the starting one. Always inside the allowed range."""
    try:
        raw = redis_conn.get(INTERVAL_KEY)
        value = int(float(raw)) if raw else INTERVAL_MS
    except Exception:
        value = INTERVAL_MS
    return max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, value))


def take_slot(redis_conn) -> bool:
    """Claim the one request slot of this interval. False if it is taken."""
    try:
        return bool(redis_conn.set(GATE_KEY, "1", nx=True, px=current_interval(redis_conn)))
    except Exception:
        return False


async def wait_for_slot(redis_conn, deadline: float) -> bool:
    """Wait (until the ``time.monotonic()`` deadline) for a request slot."""
    while time.monotonic() < deadline:
        if take_slot(redis_conn):
            return True
        await asyncio.sleep(0.2)
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


# --------------------------------------------------------------------------- #
# Health: what the poller leaves behind for scripts/health_watch.py
#
# Jagex publish no rate limit and would not announce a block. The only way to
# know we have been cut off is to notice that the answers stopped. So every
# request leaves a trace (when we last got a real answer, what the failures
# since then looked like, counts per hour) and `health_checks` turns that
# into pass/fail lines.
# --------------------------------------------------------------------------- #

# No real answer for this long, while requests are failing, is a page. A
# refusal (403, 429) is acted on quickly: that is a block. Timeouts and 5xx
# get much longer, because the hiscores go down for hours on update days and
# that needs no one's attention.
REFUSED_STALL_SECONDS = 30 * 60
OUTAGE_STALL_SECONDS = 3 * 3600
LOOP_STALL_SECONDS = 15 * 60
# Share of an hour's requests that failed, and how many requests make the
# share meaningful.
ERROR_SHARE_MAX = 0.5
# A pace this slow, for this long, means readings are going stale for everyone
# who depends on the poller, whatever the cause. On 2026-11-20 nothing said so:
# every request was being answered, there were just a twentieth as many.
SLOW_PACE_FACTOR = 3             # times the fastest pace
SLOW_PACE_SECONDS = 6 * 3600
ERROR_SHARE_MIN_REQUESTS = 40
_HOURLY_TTL = 3 * 86400


def _hour_key(now: Optional[datetime] = None) -> str:
    return HOURLY_KEY.format(hour=(now or datetime.utcnow()).strftime("%Y%m%d%H"))


def _failure_label(status: str, detail: str) -> str:
    if status == RATE_LIMITED:
        return "http 429"
    return (detail or "error")[:40]


def is_refusal(label: str) -> bool:
    """Whether a failure reads as "you are not welcome" rather than "we are
    down": any 4xx. (A 404 never gets here; it is an answer.)"""
    return label.startswith("http 4")


def record_health(redis_conn, status: str, detail: str = "", now: Optional[datetime] = None) -> None:
    """Note one request's outcome for the health check. Never raises."""
    try:
        stamp = time.time() if now is None else (now - datetime(1970, 1, 1)).total_seconds()
        hour = _hour_key(now)
        if status in (OK, NOT_FOUND):
            redis_conn.hset(HEALTH_KEY, mapping={"last_answer": stamp, "last_attempt": stamp,
                                                 "fails_since_answer": 0})
            redis_conn.hincrby(hour, status, 1)
            redis_conn.hincrby(WINDOW_KEY, "answered", 1)
        elif status in (ERROR, RATE_LIMITED):
            label = _failure_label(status, detail)
            redis_conn.hset(HEALTH_KEY, mapping={"last_attempt": stamp, "last_problem": label,
                                                 "last_problem_at": stamp})
            redis_conn.hincrby(HEALTH_KEY, "fails_since_answer", 1)
            redis_conn.hincrby(hour, f"fail:{label}", 1)
            redis_conn.hincrby(WINDOW_KEY, "failed", 1)
            if is_refusal(label):
                slow_down_for_refusal(redis_conn, label, now)
        else:
            return
        redis_conn.expire(hour, _HOURLY_TTL)
    except Exception:
        pass


def record_cycle(redis_conn, state: str, now: Optional[datetime] = None) -> None:
    """The poller's heartbeat: it ran a cycle, and what state it was in
    (``running``, ``disabled``, or the reason it may not make requests)."""
    try:
        stamp = time.time() if now is None else (now - datetime(1970, 1, 1)).total_seconds()
        redis_conn.hset(HEALTH_KEY, mapping={"cycle": stamp, "state": str(state)[:40]})
    except Exception:
        pass


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def _hash(redis_conn, key: str) -> Dict[str, str]:
    return {_text(k): _text(v) for k, v in (redis_conn.hgetall(key) or {}).items()}


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def recent_counts(redis_conn, now: datetime, hours: int = 2) -> Dict[str, int]:
    """Outcome counts over the current and previous ``hours - 1`` UTC hours."""
    from datetime import timedelta

    out: Dict[str, int] = {}
    for back in range(hours):
        for name, count in _hash(redis_conn, _hour_key(now - timedelta(hours=back))).items():
            try:
                out[name] = out.get(name, 0) + int(count)
            except ValueError:
                continue
    return out


def health_checks(redis_conn, now: Optional[datetime] = None) -> list:
    """``[(key, label, ok, detail)]`` for scripts/health_watch.py.

    Empty when the poller has never run here (nothing to judge). Four checks:
    the loop is alive; the hiscores are answering; not too many requests are
    failing; and the pace has not been slow for hours. A poller that is switched off, or resting at its daily cap,
    passes: silence it chose is not a fault.
    """
    now = now or datetime.utcnow()
    stamp = (now - datetime(1970, 1, 1)).total_seconds()
    health = _hash(redis_conn, HEALTH_KEY)
    cycle = _float(health.get("cycle"))
    if cycle is None:
        return []
    state = health.get("state") or "running"
    results = []

    loop_idle = stamp - cycle
    results.append((
        "hiscores:loop", "Hiscores poller running", loop_idle <= LOOP_STALL_SECONDS,
        f"no poller cycle for {loop_idle / 60:.0f} min. It runs inside "
        f"droptracker-player-updates: check that unit's journal for 'hiscores'"))
    if state in ("disabled", "dev instance"):
        return results

    last_answer = _float(health.get("last_answer"))
    last_attempt = _float(health.get("last_attempt"))
    fails = int(_float(health.get("fails_since_answer")) or 0)
    counts = recent_counts(redis_conn, now)
    failures = {name[5:]: n for name, n in counts.items() if name.startswith("fail:")}
    refused = {label: n for label, n in failures.items() if is_refusal(label)}
    summary = ", ".join(f"{label} x{n}" for label, n in sorted(failures.items(), key=lambda kv: -kv[1])[:4])

    answering, detail = True, ""
    if fails > 0 and last_attempt is not None:
        silent = stamp - (last_answer if last_answer is not None else last_attempt)
        limit = REFUSED_STALL_SECONDS if refused else OUTAGE_STALL_SECONDS
        if silent > limit:
            answering = False
            breaker = " The poller has paused itself and retries by itself." if breaker_open(redis_conn) else ""
            if refused:
                detail = (f"no answer from the OSRS hiscores for {silent / 60:.0f} min and the "
                          f"failures are refusals ({summary}). This looks like a BLOCK or rate limit "
                          f"on our address.{breaker} To stop asking: HISCORES_POLLER=false in .env, "
                          f"restart droptracker-player-updates. Plugin data is unaffected")
            else:
                detail = (f"no answer from the OSRS hiscores for {silent / 60:.0f} min ({summary}). "
                          f"Timeouts and 5xx usually mean the hiscores are down (routine on update "
                          f"days), not a block.{breaker} Plugin data is unaffected")
    results.append(("hiscores:answers", "OSRS hiscores answering", answering, detail))

    answered = counts.get(OK, 0) + counts.get(NOT_FOUND, 0)
    failed = sum(failures.values())
    total = answered + failed
    too_many = total >= ERROR_SHARE_MIN_REQUESTS and failed / total >= ERROR_SHARE_MAX
    results.append((
        "hiscores:errors", "OSRS hiscores error share", not too_many,
        f"{failed} of {total} hiscores requests failed in the last two hours ({summary}). "
        f"Answers are still arriving, so this is throttling or an unstable service rather "
        f"than a full block. The poller slows itself down when errors climb "
        f"(now one request per {current_interval(redis_conn)} ms)"))

    interval = current_interval(redis_conn)
    slow_since = _float(health.get("slow_since"))
    slow_for = stamp - slow_since if slow_since is not None else 0
    dragging = interval > slow_pace_ms() and slow_for > SLOW_PACE_SECONDS
    results.append((
        "hiscores:pace", "Hiscores poller pace", not dragging,
        f"the poller has been at one request per {interval} ms or slower for "
        f"{slow_for / 3600:.0f} hours (normal is {MIN_INTERVAL_MS} ms), so readings are going "
        f"stale. It slows down when requests fail or are refused and should speed up by "
        f"itself each clean hour. Recent changes: redis LRANGE {PACE_LOG_KEY} 0 10. If the "
        f"hiscores are healthy again, set {INTERVAL_KEY} lower by hand"))
    return results


def slow_pace_ms() -> int:
    return MIN_INTERVAL_MS * SLOW_PACE_FACTOR


# --------------------------------------------------------------------------- #
# Pace: faster while the hiscores are happy, slower the moment they are not
# --------------------------------------------------------------------------- #

TUNE_EVERY_SECONDS = 3600
TUNE_MIN_REQUESTS = 200          # fewer than this says nothing about a failure share
SPEED_UP_STEP_MS = 100           # how much faster after a clean hour
SPEED_UP_MAX_FAIL_SHARE = 0.005  # "clean": at most 0.5% of requests failed
SLOW_DOWN_FAIL_SHARE = 0.03      # 3% failing is the hiscores struggling, or us being throttled
SLOW_DOWN_FACTOR = 1.5
REFUSAL_FACTOR = 2.0             # a 403/429 doubles the interval on the spot
REFUSAL_QUIET_SECONDS = 12 * 3600  # no speeding up this long after a refusal
REFUSAL_CAREFUL_SECONDS = 7 * 86400  # and only in small steps for a week after one
# Errors that are not refusals (502, 504, timeouts) are the hiscores having
# trouble, not a limit on us. They never slow us past the pace we started at,
# and a clean hour undoes a whole slow-down. On 2026-11-20 five hours of 502s
# took the interval from 500 ms to 5.7 s, and at 100 ms an hour it would have
# taken two days to come back; readings went stale for everyone meanwhile.
FAILURE_MAX_INTERVAL_MS = max(INTERVAL_MS, MIN_INTERVAL_MS)


def _set_interval(redis_conn, value: float, reason: str, now: Optional[datetime] = None) -> int:
    new = int(max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, round(value))))
    old = current_interval(redis_conn)
    if new != old:
        redis_conn.set(INTERVAL_KEY, new)
        stamp = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M")
        redis_conn.lpush(PACE_LOG_KEY, f"{stamp} {old}ms -> {new}ms: {reason}")
        redis_conn.ltrim(PACE_LOG_KEY, 0, 99)
        # When the slow spell began, for the pace health check. "" is "not slow".
        slow = slow_pace_ms()
        since = _float(_hash(redis_conn, HEALTH_KEY).get("slow_since"))
        if new > slow and since is None:
            at = time.time() if now is None else (now - datetime(1970, 1, 1)).total_seconds()
            redis_conn.hset(HEALTH_KEY, mapping={"slow_since": at})
        elif new <= slow and since is not None:
            redis_conn.hset(HEALTH_KEY, mapping={"slow_since": ""})
    return new


def slow_down_for_refusal(redis_conn, label: str, now: Optional[datetime] = None) -> None:
    """We were told no. Halve the pace at once and remember when. Never raises."""
    try:
        stamp = time.time() if now is None else (now - datetime(1970, 1, 1)).total_seconds()
        redis_conn.hset(HEALTH_KEY, mapping={"last_refusal": stamp})
        _set_interval(redis_conn, current_interval(redis_conn) * REFUSAL_FACTOR,
                      f"refused ({label})", now)
    except Exception:
        pass


def tune_pace(redis_conn, now: Optional[datetime] = None) -> Optional[str]:
    """Adjust the request interval from the last hour's outcomes. Called every
    poller cycle; does something at most once an hour, and only with enough
    requests to judge by. Returns a line describing a change, else None.

    - 3% or more failed: slow down by half again, but never past the starting
      pace (only a refusal goes slower than that).
    - at most 0.5% failed, and no refusal in the last 12 hours: undo one
      slow-down (at least 100 ms faster), down to the floor. Within a week of
      a refusal: 100 ms faster only.
    - anything between: hold.
    """
    try:
        now = now or datetime.utcnow()
        stamp = (now - datetime(1970, 1, 1)).total_seconds()
        health = _hash(redis_conn, HEALTH_KEY)
        last = _float(health.get("last_tune"))
        if last is not None and stamp - last < TUNE_EVERY_SECONDS:
            return None
        window = _hash(redis_conn, WINDOW_KEY)
        answered, failed = int(window.get("answered") or 0), int(window.get("failed") or 0)
        total = answered + failed
        if last is None:
            # First run: start the clock, judge at the next one.
            redis_conn.hset(HEALTH_KEY, mapping={"last_tune": stamp})
            redis_conn.delete(WINDOW_KEY)
            return None
        if total < TUNE_MIN_REQUESTS:
            return None
        redis_conn.hset(HEALTH_KEY, mapping={"last_tune": stamp})
        redis_conn.delete(WINDOW_KEY)
        share = failed / total
        before = current_interval(redis_conn)
        refused_at = _float(health.get("last_refusal"))
        if share >= SLOW_DOWN_FAIL_SHARE:
            slower = min(before * SLOW_DOWN_FACTOR, max(before, FAILURE_MAX_INTERVAL_MS))
            after = _set_interval(redis_conn, slower,
                                  f"{failed} of {total} requests failed", now)
        elif share <= SPEED_UP_MAX_FAIL_SHARE and (
                refused_at is None or stamp - refused_at >= REFUSAL_QUIET_SECONDS):
            careful = refused_at is not None and stamp - refused_at < REFUSAL_CAREFUL_SECONDS
            faster = before - SPEED_UP_STEP_MS
            if not careful:
                faster = min(faster, before / SLOW_DOWN_FACTOR)
            after = _set_interval(redis_conn, faster,
                                  f"clean hour ({failed} of {total} failed)", now)
        else:
            after = before
        return f"pace {before}ms -> {after}ms ({failed} of {total} failed)" if after != before else None
    except Exception:
        return None


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
    # Several lookups share the one slot per interval, so at a slow pace the
    # last in line waits several intervals. Giving up early reads as "blocked"
    # and ends the whole batch.
    wait_seconds = max(wait_seconds, current_interval(redis_conn) / 1000.0 * 8)
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
                record_health(redis_conn, RATE_LIMITED)
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
    record_health(redis_conn, result.status, result.detail)
    return result
