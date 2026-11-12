"""Who the hiscores poller looks up, and when. Pure rules, no I/O.

The poller is never a sweep of everyone. It looks up only players an event
is waiting on and the plugin is not already telling us about:

- **start**: once, around the moment a participant's scoring begins, so there
  is a baseline to measure from;
- **end**: once, after the event closes, so there is a final value;
- **rotation**: in between, on a cadence set by how active the player has
  been, because a hiscores entry only moves when the player logs out.

Start and end readings are *pinned*: retention never thins them away.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# Event task types whose progress the hiscores can show (the same set the
# WOM reconciler tracks).
RELEVANT_TASK_TYPES = ("xp_target", "skill_target", "kc_target", "competition")

# A participant whose plugin talked to us this recently needs no lookup: the
# plugin reports exact XP and KC, sooner than the hiscores would.
PLUGIN_COVERED = timedelta(hours=6)

START_LEAD = timedelta(minutes=30)     # begin taking baselines this long before the start
PIN_WINDOW = timedelta(hours=12)       # how long a missed start/end reading is still worth taking
ACTIVE_FOR = timedelta(hours=24)       # "active" = a reading changed this recently
QUIET_FOR = timedelta(hours=72)
ACTIVE_INTERVAL = timedelta(hours=2)
QUIET_INTERVAL = timedelta(hours=6)
DORMANT_INTERVAL = timedelta(hours=24)
NOT_FOUND_BACKOFF = timedelta(hours=24)  # a name the hiscores do not know is not retried sooner

START, END, ROTATION = "start", "end", "rotation"
_PRIORITY = {END: 0, START: 1, ROTATION: 2}


@dataclass(frozen=True)
class Due:
    reason: str
    pinned: bool
    priority: int
    overdue_seconds: float = 0.0


def rotation_interval(now: datetime, last_changed: Optional[datetime],
                      first_polled: Optional[datetime] = None) -> timedelta:
    """How long to leave a participant alone between rotation lookups."""
    # Until a reading has had the chance to change, assume the player is active.
    reference = last_changed or first_polled
    if reference is None or now - reference <= ACTIVE_FOR:
        return ACTIVE_INTERVAL
    if now - reference <= QUIET_FOR:
        return QUIET_INTERVAL
    return DORMANT_INTERVAL


def due(now: datetime, *, starts_at: Optional[datetime], ends_at: Optional[datetime],
        joined_at: Optional[datetime], plugin_seen_at: Optional[datetime],
        last_polled: Optional[datetime], last_changed: Optional[datetime],
        first_polled: Optional[datetime], not_found_at: Optional[datetime],
        has_start: bool, has_end: bool) -> Optional[Due]:
    """Whether to look this participant up now, and why. None = leave alone.

    A participant covered by the plugin is still returned for a start or end
    reading (the caller pins the plugin's own latest row instead of asking
    the hiscores); they are simply never part of the rotation.
    """
    # Scoring starts at the event's start, or when a late joiner joined.
    begins = max([t for t in (starts_at, joined_at) if t is not None], default=None)
    if begins is not None and now < begins - START_LEAD:
        return None
    blocked = not_found_at is not None and now - not_found_at < NOT_FOUND_BACKOFF

    if ends_at is not None and now >= ends_at:
        if has_end or now - ends_at > PIN_WINDOW or blocked:
            return None
        return Due(END, True, _PRIORITY[END], (now - ends_at).total_seconds())

    if blocked:
        return None
    if not has_start and begins is not None and now - begins <= PIN_WINDOW:
        return Due(START, True, _PRIORITY[START], (now - (begins - START_LEAD)).total_seconds())

    if plugin_seen_at is not None and now - plugin_seen_at <= PLUGIN_COVERED:
        return None
    if last_polled is None:
        return Due(ROTATION, False, _PRIORITY[ROTATION], float("inf"))
    waited = now - last_polled
    interval = rotation_interval(now, last_changed, first_polled)
    if waited < interval:
        return None
    return Due(ROTATION, False, _PRIORITY[ROTATION], (waited - interval).total_seconds())


def order(candidates):
    """Most urgent first: end readings, then start readings, then the rotation
    by how overdue each is. ``candidates`` are ``(Due, anything)`` pairs."""
    return sorted(candidates, key=lambda pair: (pair[0].priority, -pair[0].overdue_seconds))
