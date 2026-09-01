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

    def _work():
        session = SessionLocal()
        try:
            player_ids = resolve(session)
            if player_ids is None:
                return None, None
            # Price only once the page size is known — cost is per player.
            decision = check_and_charge(key["key_id"], key["limits"],
                                        sect.cost_of(requested, len(player_ids)))
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
