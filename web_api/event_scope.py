"""Staff-hosted clan-vs-clan events (web119a): who runs what.

A ``clan_vs_clan`` event normally has a HOST group (``web_events.group_id``)
whose admins set it up, and every accepted clan's admins co-manage it. A
clan_vs_clan event with NO host group is **staff-hosted**: DropTracker staff
own the whole setup (tasks, board, schedule, scoring, review), and each
participating clan controls only its own side — its roster (picked from its
members' sign-ups, within ``clan_roster_min``/``clan_roster_max``), its team
leaders and its own Discord destinations.

Derived, never stored: ``mode == 'clan_vs_clan' and group_id is None``. A
global event could never become clan-vs-clan before web119a (the create and
mode-change routes refused it), so no existing row changes meaning.

Lives in web_api (not services/) for the same reason as event_leadership.py:
the unit-test conftest stubs the whole ``services`` package. Stdlib only.
"""
from __future__ import annotations

from typing import Optional

#: Upper bound on either roster limit — a sanity cap, not a product rule.
MAX_CLAN_ROSTER = 500


def is_staff_hosted(ev) -> bool:
    """Whether ``ev`` is a staff-hosted clan-vs-clan event."""
    return (
        (getattr(ev, "mode", None) or "standard") == "clan_vs_clan"
        and getattr(ev, "group_id", None) is None
    )


def roster_limits(ev) -> tuple[Optional[int], Optional[int]]:
    """``(min, max)`` players per clan; ``None`` = unbounded. Only meaningful
    on staff-hosted events (other events never enforce them)."""
    if not is_staff_hosted(ev):
        return None, None
    lo = getattr(ev, "clan_roster_min", None)
    hi = getattr(ev, "clan_roster_max", None)
    return (int(lo) if lo else None), (int(hi) if hi else None)


def validate_roster_limits(lo, hi) -> Optional[str]:
    """User-facing reason the pair is invalid, or ``None`` when it's fine.
    Either may be ``None`` (no limit); set values are whole numbers from 1 to
    :data:`MAX_CLAN_ROSTER`, and the minimum can't exceed the maximum."""
    for label, v in (("minimum", lo), ("maximum", hi)):
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            return f"The roster {label} must be a whole number."
        if not 1 <= v <= MAX_CLAN_ROSTER:
            return f"The roster {label} must be between 1 and {MAX_CLAN_ROSTER}."
    if lo is not None and hi is not None and lo > hi:
        return "The roster minimum can't be larger than the maximum."
    return None


def capacity_problem(ev, current: int, adding: int) -> Optional[str]:
    """Reason adding ``adding`` players to a clan roster of ``current`` would
    break the event's maximum, or ``None`` when it fits."""
    _, hi = roster_limits(ev)
    if hi is None or adding <= 0:
        return None
    if current + adding > hi:
        room = max(hi - current, 0)
        if room == 0:
            return f"This clan's roster is full ({hi} players)."
        return (f"This clan's roster has room for {room} more "
                f"{'player' if room == 1 else 'players'} (the limit is {hi}).")
    return None


def clan_roster_locked(ev, effective_status: str) -> bool:
    """Whether clan leaders are locked out of roster changes right now:
    staff-hosted, the lock is on, and the event has started (or ended)."""
    if not is_staff_hosted(ev):
        return False
    if effective_status == "past":
        return True
    locked = getattr(ev, "clan_roster_locked_at_start", True)
    return bool(True if locked is None else locked) and effective_status == "active"
