"""The shared request pipeline every data route runs through.

Ordering matters and is the same for every endpoint:

    parse  ->  price  ->  charge  ->  reserve a slot  ->  work  ->  meter

Cost is computed and charged *before* the database is touched, so a request
that exceeds its budget is refused without doing the work. The concurrency
slot is taken next, so one caller cannot occupy every worker even while inside
budget. Only then does the (blocking, thread-offloaded) ORM work happen.

The session is opened inside the worker thread and always closed there — a
leaked session pins a connection from the small pool and, on a shared server,
an InnoDB read view. Statement-ceiling timeouts become 503 rather than 500:
"too heavy, try a smaller window" is actionable; "internal error" is not.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, List

from quart import g, jsonify, request

from data_api import sections as sect
from data_api.core import SessionLocal, is_statement_timeout
from data_api.limits import Concurrency, check_and_charge
from data_api import usage


def _json(payload, status: int, headers: dict | None = None):
    response = jsonify(payload)
    response.status_code = status
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


def parse_sections():
    """``(sections, error_response)`` from ``?include=``."""
    try:
        return sect.parse_include(request.args.get("include", "")), None
    except ValueError as exc:
        return None, _json({
            "error": "unknown_section",
            "detail": f"No section named '{exc.args[0]}'.",
            "available": list(sect.ALL_SECTION_KEYS),
        }, 400)


def int_param(name: str, default: int, minimum: int, maximum: int):
    """``(value, error_response)`` for one integer query parameter.

    A parameter that is present but not an integer is a ``400`` naming it, for
    the same reason an unknown section is: silently substituting the default
    answers a question the caller did not ask, and they find out from the data
    rather than from the status code. An *in*-range-able value is clamped
    instead — the published maximum is a ceiling, not a typo.
    """
    raw = request.args.get(name)
    if raw is None or not raw.strip():
        return default, None
    try:
        value = int(raw.strip())
    except ValueError:
        return None, _json({
            "error": "malformed_parameter",
            "detail": f"'{name}' must be an integer; got '{raw}'.",
        }, 400)
    return max(minimum, min(value, maximum)), None


def page_params(default_page: int, max_page: int):
    """``(limit, cursor, error_response)`` for the cursor-paginated endpoints.

    One implementation for every listing route: three copies of this parsing
    is how ``limit`` ends up meaning something different on one of them.
    """
    limit, error = int_param("limit", default_page, 1, max_page)
    if error is not None:
        return None, None, error
    # Cursor is a player/group id, so any non-negative int is in range.
    cursor, error = int_param("cursor", 0, 0, 2 ** 63 - 1)
    if error is not None:
        return None, None, error
    return limit, cursor, None


def drops_window():
    """``(since, until, per_player, error)`` — the drop feed's bounds.

    ``since``/``until`` are unix seconds (UTC). ``until`` defaults to now and
    is clamped to it; ``since`` defaults to 24 hours before ``until`` and is
    clamped so the window never exceeds :data:`sections.DROPS_MAX_WINDOW_HOURS`
    — the published ceiling, so asking past it narrows the window rather than
    refusing the call. A window that runs backwards (``since >= until``) is a
    ``400``: there is no sensible clamp for a question that cannot be answered.

    ``max_drops`` caps rows per player (clamped, like ``limit``).
    """
    from datetime import datetime, timedelta

    from data_api import sections as sect

    import calendar

    now = datetime.utcnow().replace(microsecond=0)
    now_ts = calendar.timegm(now.timetuple())

    until_ts, error = int_param("until", now_ts, 0, now_ts)
    if error is not None:
        return None, None, None, error
    default_since = until_ts - sect.DROPS_DEFAULT_WINDOW_HOURS * 3600
    since_ts, error = int_param("since", default_since, 0, 2 ** 40)
    if error is not None:
        return None, None, None, error
    if since_ts >= until_ts:
        return None, None, None, _json({
            "error": "malformed_parameter",
            "detail": "'since' must be earlier than 'until'.",
        }, 400)
    floor = until_ts - sect.DROPS_MAX_WINDOW_HOURS * 3600
    since_ts = max(since_ts, floor)

    per_player, error = int_param("max_drops", sect.DROPS_DEFAULT_PER_PLAYER,
                                  1, sect.DROPS_MAX_PER_PLAYER)
    if error is not None:
        return None, None, None, error

    since = datetime.utcfromtimestamp(since_ts)
    until = datetime.utcfromtimestamp(until_ts)
    return since, until, per_player, None


def date_hour_range():
    """``(start, end, days, error)`` — the rollup window, as 'YYYY-MM-DD-HH'.

    Defaults to the last 30 days. Ranging on ``date_hour`` is the only shape
    the rollup indexes support well; the window is capped so one request
    cannot ask for a scan of all history.
    """
    from datetime import datetime, timedelta

    max_days = 366
    days, error = int_param("days", 30, 1, max_days)
    if error is not None:
        return None, None, None, error
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d-%H"), end.strftime("%Y-%m-%d-%H"), days, None


async def serve(endpoint: str, resolve: Callable, build: Callable,
                not_found_detail: str | None = None):
    """Run one data request end to end.

    ``resolve(session)`` returns the list of player ids this request will read
    (cheap: a PK lookup or a roster page), or ``None`` for "no such thing" —
    which becomes a 404 carrying ``not_found_detail``. ``build(session,
    player_ids, ctx)`` returns the response body. Both run in the same worker
    thread and share one session.
    """
    key = g.api_key
    started = time.perf_counter()

    requested, error = parse_sections()
    if error is not None:
        return error

    start_hour, end_hour, days, error = date_hour_range()
    if error is not None:
        return error
    # Parsed through int_param like the rest: a bare int() here made '?top=abc'
    # an unhandled ValueError, i.e. a 500 for a caller's typo.
    per_player_limit, error = int_param("top", 10, 1, 50)
    if error is not None:
        return error
    ctx = {
        "date_hour_range": (start_hour, end_hour),
        "days": days,
        "sections": requested,
        "per_player_limit": per_player_limit,
    }
    if "drops" in requested:
        # Parsed only when asked for: a caller of the profile sections should
        # not be refused over a drop-feed parameter they never used.
        since, until, per_player, error = drops_window()
        if error is not None:
            return error
        ctx["drops_window"] = (since, until)
        ctx["drops_per_player"] = per_player
        raw_npc = request.args.get("npc")
        if raw_npc is not None:
            if not raw_npc.strip():
                return _json({
                    "error": "malformed_parameter",
                    "detail": "'npc' must name a boss or a Wise Old Man boss slug "
                              "(e.g. barrows_chests).",
                }, 400)
            ctx["drops_npc"] = raw_npc.strip()

    def _work():
        session = SessionLocal()
        try:
            player_ids = resolve(session)
            if player_ids is None:
                return None, None
            if ctx.get("drops_npc"):
                # Resolved before pricing: a name DropTracker does not know is
                # the caller's mistake, and it must not cost them budget.
                npc_ids = sect.resolve_npc_ids(session, ctx["drops_npc"])
                if not npc_ids:
                    return "bad_npc", None
                ctx["drops_npc_ids"] = npc_ids
            # Price only once the page size is known — cost is per player.
            decision = check_and_charge(key["key_id"], key["limits"],
                                        sect.cost_of(requested, len(player_ids), ctx))
            if not decision.allowed:
                return "limited", decision
            with Concurrency(key["key_id"], key["limits"]["max_concurrency"]) as slot:
                if not slot.acquired:
                    return "busy", decision
                return "ok", (build(session, player_ids, ctx), decision)
        finally:
            session.close()

    try:
        outcome, payload = await asyncio.to_thread(_work)
    except Exception as exc:
        duration = (time.perf_counter() - started) * 1000
        if is_statement_timeout(exc):
            usage.record(key["key_id"], endpoint, 503, duration, 0)
            return _json({
                "error": "query_too_heavy",
                "detail": "This request exceeded the server's query time limit. "
                          "Narrow the window (?days=) or request fewer sections.",
            }, 503)
        usage.record(key["key_id"], endpoint, 500, duration, 0)
        raise

    duration = (time.perf_counter() - started) * 1000

    if outcome is None:
        usage.record(key["key_id"], endpoint, 404, duration, 0)
        body = {"error": "not_found"}
        if not_found_detail:
            body["detail"] = not_found_detail
        return _json(body, 404)

    if outcome == "bad_npc":
        usage.record(key["key_id"], endpoint, 400, duration, 0)
        return _json({
            "error": "malformed_parameter",
            "detail": f"No boss called '{ctx['drops_npc']}' is known to DropTracker. "
                      "Pass a boss name or a Wise Old Man boss slug (e.g. barrows_chests).",
        }, 400)

    if outcome == "limited":
        decision = payload
        usage.record(key["key_id"], endpoint, 429, duration, 0, limited=True)
        headers = dict(decision.headers)
        if decision.retry_after:
            headers["Retry-After"] = str(decision.retry_after)
        return _json({"error": "rate_limited", "limit": decision.reason,
                      "detail": f"Exceeded the {decision.reason} budget for this key."},
                     429, headers)

    if outcome == "busy":
        decision = payload
        usage.record(key["key_id"], endpoint, 429, duration, 0, limited=True)
        headers = dict(decision.headers)
        headers["Retry-After"] = "1"
        return _json({"error": "too_many_concurrent_requests",
                      "detail": "This key already has its maximum number of "
                                "requests in flight."}, 429, headers)

    body, decision = payload
    cost = int(decision.headers.get("X-RateLimit-Cost", 0))
    players = body.get("count", 1) if isinstance(body, dict) else 1
    usage.record(key["key_id"], endpoint, 200, duration, cost, players=players)

    if usage.touch_last_used(key["key_id"]):
        asyncio.get_running_loop().run_in_executor(None, _touch, key["key_id"])

    headers = dict(decision.headers)
    headers["Cache-Control"] = "private, max-age=30"
    headers["X-Response-Time-Ms"] = str(int(duration))
    return _json(body, 200, headers)


async def serve_fixed(endpoint: str, cost: int, build: Callable,
                      not_found_detail: str | None = None,
                      cache_seconds: int = 3600):
    """The same pipeline for an endpoint that is not about players.

    Reference data (the collection log structure) has no page and no
    sections, so it is priced at a flat ``cost`` and skips the include/window
    parsing — but it still charges, still takes a concurrency slot, still
    meters, and still turns a statement timeout into a 503. ``build(session)``
    returns the body, or ``None`` for "nothing published yet" → 404.
    """
    key = g.api_key
    started = time.perf_counter()

    def _work():
        decision = check_and_charge(key["key_id"], key["limits"], cost)
        if not decision.allowed:
            return "limited", decision
        with Concurrency(key["key_id"], key["limits"]["max_concurrency"]) as slot:
            if not slot.acquired:
                return "busy", decision
            session = SessionLocal()
            try:
                body = build(session)
            finally:
                session.close()
            if body is None:
                return None, decision
            return "ok", (body, decision)

    try:
        outcome, payload = await asyncio.to_thread(_work)
    except Exception as exc:
        duration = (time.perf_counter() - started) * 1000
        if is_statement_timeout(exc):
            usage.record(key["key_id"], endpoint, 503, duration, 0)
            return _json({
                "error": "query_too_heavy",
                "detail": "This request exceeded the server's query time limit.",
            }, 503)
        usage.record(key["key_id"], endpoint, 500, duration, 0)
        raise

    duration = (time.perf_counter() - started) * 1000
    if outcome == "limited":
        usage.record(key["key_id"], endpoint, 429, duration, 0, limited=True)
        headers = dict(payload.headers)
        if payload.retry_after:
            headers["Retry-After"] = str(payload.retry_after)
        return _json({"error": "rate_limited", "limit": payload.reason,
                      "detail": f"Exceeded the {payload.reason} budget for this key."},
                     429, headers)
    if outcome == "busy":
        usage.record(key["key_id"], endpoint, 429, duration, 0, limited=True)
        headers = dict(payload.headers)
        headers["Retry-After"] = "1"
        return _json({"error": "too_many_concurrent_requests",
                      "detail": "This key already has its maximum number of "
                                "requests in flight."}, 429, headers)
    if outcome is None:
        usage.record(key["key_id"], endpoint, 404, duration, 0)
        body = {"error": "not_found"}
        if not_found_detail:
            body["detail"] = not_found_detail
        return _json(body, 404)

    body, decision = payload
    usage.record(key["key_id"], endpoint, 200, duration, cost, players=0)
    if usage.touch_last_used(key["key_id"]):
        asyncio.get_running_loop().run_in_executor(None, _touch, key["key_id"])
    headers = dict(decision.headers)
    headers["Cache-Control"] = f"private, max-age={int(cache_seconds)}"
    headers["X-Response-Time-Ms"] = str(int(duration))
    return _json(body, 200, headers)


def _touch(key_id: int) -> None:
    """Write ``last_used_at``. Rate-limited to ~1/min by the Redis gate above.

    Uses the shared engine, not this service's read-only one: the data API's
    DB user has SELECT only, by design.
    """
    try:
        from datetime import datetime

        from db.models import ApiKey, Session

        session = Session()
        try:
            session.query(ApiKey).filter(ApiKey.id == key_id).update(
                {"last_used_at": datetime.utcnow()}, synchronize_session=False)
            session.commit()
        finally:
            session.close()
    except Exception:
        pass
