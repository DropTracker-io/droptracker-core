# utils/event_window.py — when an event's scoring window opens.
#
# A scheduled draft goes live on the events worker's lifecycle sweep, which
# runs about once a minute, so ``activated_at`` routinely lands a few seconds
# to a minute after ``starts_at``. The window used to open at
# max(starts_at, activated_at), which threw away every submission in that gap
# (event 81, 2026-09-25: a Barrows piece 26s after the scheduled start). A
# late activation within the grace below is the scheduler catching up, so the
# window still opens at ``starts_at``; services.event_engine parks the gap's
# submissions and replays them once the event is live.
#
# Stdlib only, like utils.vestige_rings: the engine, the lifecycle, Conquest
# and the WOM competition sync all read this, and the unit-test conftest stubs
# the whole ``services`` package, so the shared definition can't live there.

from __future__ import annotations

from datetime import timedelta

# An activation this long after starts_at, or sooner, still counts from
# starts_at. Later than that, the event runs from when it actually started.
LATE_START_GRACE_SECONDS = 30 * 60


def effective_window_start(starts_at, activated_at):
    """The scheduled start, narrowed by the activation stamp, except that an
    activation within :data:`LATE_START_GRACE_SECONDS` after ``starts_at``
    keeps ``starts_at``. An early "Start now" (activated before starts_at)
    keeps the scheduled start, as it always has. None when neither is set."""
    if starts_at is None:
        return activated_at
    if activated_at is None or activated_at <= starts_at:
        return starts_at
    if activated_at - starts_at <= timedelta(seconds=LATE_START_GRACE_SECONDS):
        return starts_at
    return activated_at
