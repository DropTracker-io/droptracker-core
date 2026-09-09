"""Request-duration accounting for the slow-request log in api/__init__.py.

``GET /notifications?wait=N`` deliberately parks a request for up to 25s
waiting for an inbox push. Measured end-to-end, every expired hold is a
~25,000ms request, and the after_request slow-request log fired on all of
them — roughly 1.3M journal lines a day (two per poll) that buried the
genuinely slow requests on other paths.

Handlers that hold on purpose call :func:`record_hold` with the time they
spent idle; the logger then judges the *work* (total minus held) against the
slow threshold and prints both numbers, so a /notifications request that is
slow for a real reason (a stalled identity lookup, say) still shows up.

Pure functions over the request-context ``g`` object — no Quart import, so
this stays loadable from a file path in unit tests.
"""

HELD_ATTR = "held_ms"


def record_hold(g, seconds) -> None:
    """Add deliberate idle time (in seconds) to the current request's tally."""
    try:
        held = float(seconds) * 1000.0
    except (TypeError, ValueError):
        return
    if held <= 0:
        return
    setattr(g, HELD_ATTR, held_ms(g) + held)


def held_ms(g) -> float:
    """Deliberate idle time recorded so far on this request, in ms (>= 0)."""
    try:
        return max(0.0, float(getattr(g, HELD_ATTR, 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def work_ms(total_ms, held) -> float:
    """Time the request spent doing work: total minus hold, floored at zero."""
    try:
        held = max(0.0, float(held or 0.0))
    except (TypeError, ValueError):
        held = 0.0
    return max(0.0, float(total_ms) - held)


def describe(total_ms, held) -> str:
    """``'812.40 ms'`` — or, when part of it was a hold,
    ``'25004.79 ms (24998.10 ms held, 6.69 ms work)'``."""
    total_ms = float(total_ms)
    if not held or held <= 0:
        return f"{total_ms:.2f} ms"
    return (f"{total_ms:.2f} ms ({float(held):.2f} ms held, "
            f"{work_ms(total_ms, held):.2f} ms work)")
