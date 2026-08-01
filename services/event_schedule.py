"""Recurring event activation schedules (web82a).

An event may carry a ``schedule_config`` (JSON on ``web_events``) describing
WHEN, inside its overall ``starts_at → ends_at`` span, scoring is actually
open — e.g. "every weekend for the whole month, all weekends counting as one
event". The rule is compiled ("materialized") into an explicit, ordered list
of ``[start, end)`` windows stored in ``web_event_windows``; every consumer
(the scoring gate, WOM reconciler, loot totals, Discord/board/plugin
displays) reads the compiled windows and never re-interprets the rule.

Between windows the event stays ``active`` — Discord channels, standings and
pages all stay up — but submissions timestamped outside every window credit
nothing (the same freeze the overall window already applies).

All datetimes here are naive UTC, matching the repo-wide convention (the
production host runs Etc/UTC, so ``datetime.now()`` == UTC). Rules are
defined against the UTC clock — OSRS "game time" — deliberately: one
unambiguous clock for international clans, no DST surprises.

Config shape (normalized by :func:`validate_config`)::

    {"v": 1, "tz": "UTC", "rule": {...}}

with ``rule`` discriminated on ``type``:

- ``weekly`` — one or more day-of-week + time-of-day spans, repeating every
  ``interval_weeks`` (1 = every week), or only the ``month_ordinal``-th
  occurrence of each calendar month (1..4, or -1 for the last)::

      {"type": "weekly", "interval_weeks": 1, "month_ordinal": null,
       "windows": [{"start_dow": 5, "start_time": "00:00",
                    "end_dow": 0, "end_time": "00:00"}]}

  (``dow``: 0=Monday .. 6=Sunday, Python ``weekday()`` numbering. The example
  is a full weekend: Saturday 00:00 → Monday 00:00.)

- ``daily`` — the same time-of-day window every day; an end at/before the
  start crosses midnight::

      {"type": "daily", "start_time": "19:00", "end_time": "23:00"}

- ``custom`` — an explicit hand-entered list of one-off windows (unix
  seconds), the escape hatch for anything the rules can't express::

      {"type": "custom", "windows": [{"start": 1791158400, "end": 1791331200}]}

Module-level imports are stdlib-only (the unit tests load this file directly
under the conftest ``db``/``services`` stubs). Anything DB-shaped lives in
the callers.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

SCHEDULE_VERSION = 1

# Hard ceilings — a runaway rule must produce a clear 422, not a million rows.
MAX_MATERIALIZED_WINDOWS = 600
MAX_WEEKLY_SPECS = 7
MAX_CUSTOM_WINDOWS = 120
MAX_INTERVAL_WEEKS = 8
MONTH_ORDINALS = (1, 2, 3, 4, -1)

_DOW_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

Window = Tuple[datetime, datetime]


class ScheduleError(Exception):
    """A schedule config is invalid or cannot materialize. ``detail`` is a
    user-facing sentence (routes map it onto a 422 problem)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------- #
# Validation / normalization
# --------------------------------------------------------------------------- #

def _parse_hhmm(value, field: str) -> Tuple[int, int]:
    if not isinstance(value, str):
        raise ScheduleError(f"'{field}' must be an HH:MM time string.")
    parts = value.strip().split(":")
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise ScheduleError(f"'{field}' must be an HH:MM time string.")
    if len(parts) != 2 or not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ScheduleError(f"'{field}' must be a valid 24h HH:MM time.")
    return hh, mm


def _fmt_hhmm(hh: int, mm: int) -> str:
    return f"{hh:02d}:{mm:02d}"


def _parse_dow(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 6):
        raise ScheduleError(f"'{field}' must be a weekday number 0 (Mon) – 6 (Sun).")
    return value


def validate_config(raw) -> Optional[dict]:
    """Validate a client-supplied schedule object and return the normalized
    dict to store (``None`` for ``None``/``{}`` — a continuous event). Raises
    :class:`ScheduleError` on anything malformed."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ScheduleError("'schedule' must be an object or null.")
    if not raw:
        return None
    tz = raw.get("tz") or "UTC"
    if tz != "UTC":
        raise ScheduleError("Only the UTC clock is supported for schedules (OSRS game time).")
    rule = raw.get("rule")
    if not isinstance(rule, dict):
        raise ScheduleError("'schedule.rule' must be an object.")
    rtype = rule.get("type")

    if rtype == "weekly":
        specs = rule.get("windows")
        if not isinstance(specs, list) or not (1 <= len(specs) <= MAX_WEEKLY_SPECS):
            raise ScheduleError(
                f"A weekly schedule needs 1–{MAX_WEEKLY_SPECS} day/time windows.")
        norm_specs = []
        for i, spec in enumerate(specs):
            if not isinstance(spec, dict):
                raise ScheduleError("Each weekly window must be an object.")
            sd = _parse_dow(spec.get("start_dow"), f"windows[{i}].start_dow")
            ed = _parse_dow(spec.get("end_dow"), f"windows[{i}].end_dow")
            sh, sm = _parse_hhmm(spec.get("start_time"), f"windows[{i}].start_time")
            eh, em = _parse_hhmm(spec.get("end_time"), f"windows[{i}].end_time")
            norm_specs.append({
                "start_dow": sd, "start_time": _fmt_hhmm(sh, sm),
                "end_dow": ed, "end_time": _fmt_hhmm(eh, em),
            })
        interval = rule.get("interval_weeks", 1)
        if interval is None:
            interval = 1
        if (isinstance(interval, bool) or not isinstance(interval, int)
                or not (1 <= interval <= MAX_INTERVAL_WEEKS)):
            raise ScheduleError(
                f"'interval_weeks' must be 1–{MAX_INTERVAL_WEEKS}.")
        ordinal = rule.get("month_ordinal")
        if ordinal is not None:
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) \
                    or ordinal not in MONTH_ORDINALS:
                raise ScheduleError(
                    "'month_ordinal' must be 1–4 (the Nth occurrence each "
                    "month) or -1 (the last), or null.")
            if interval != 1:
                raise ScheduleError(
                    "Use either 'interval_weeks' or 'month_ordinal', not both.")
        norm_rule = {"type": "weekly", "windows": norm_specs,
                     "interval_weeks": interval, "month_ordinal": ordinal}

    elif rtype == "daily":
        sh, sm = _parse_hhmm(rule.get("start_time"), "start_time")
        eh, em = _parse_hhmm(rule.get("end_time"), "end_time")
        if (sh, sm) == (eh, em):
            raise ScheduleError(
                "A daily window's start and end times are identical — that's "
                "a continuous event; remove the schedule instead.")
        norm_rule = {"type": "daily",
                     "start_time": _fmt_hhmm(sh, sm), "end_time": _fmt_hhmm(eh, em)}

    elif rtype == "custom":
        wins = rule.get("windows")
        if not isinstance(wins, list) or not (1 <= len(wins) <= MAX_CUSTOM_WINDOWS):
            raise ScheduleError(
                f"A custom schedule needs 1–{MAX_CUSTOM_WINDOWS} windows.")
        norm_wins = []
        for i, win in enumerate(wins):
            if not isinstance(win, dict):
                raise ScheduleError("Each custom window must be an object.")
            try:
                start = int(win.get("start"))
                end = int(win.get("end"))
            except (TypeError, ValueError):
                raise ScheduleError(
                    f"windows[{i}] needs integer 'start' and 'end' unix timestamps.")
            if end <= start:
                raise ScheduleError(
                    f"windows[{i}] ends at or before it starts.")
            if end - start > 366 * 24 * 3600:
                raise ScheduleError(f"windows[{i}] is longer than a year.")
            norm_wins.append({"start": start, "end": end})
        norm_wins.sort(key=lambda w: w["start"])
        norm_rule = {"type": "custom", "windows": norm_wins}

    else:
        raise ScheduleError(
            "'schedule.rule.type' must be one of 'weekly', 'daily', 'custom'.")

    return {"v": SCHEDULE_VERSION, "tz": "UTC", "rule": norm_rule}


def parse_config(raw_text) -> Optional[dict]:
    """The stored ``schedule_config`` JSON as a dict, or ``None`` when unset
    or corrupt (a corrupt config must degrade to 'continuous', never raise —
    the scoring gate simply stops narrowing)."""
    if not raw_text:
        return None
    if isinstance(raw_text, dict):
        return raw_text
    try:
        import json

        data = json.loads(raw_text)
        return data if isinstance(data, dict) and data.get("rule") else None
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Materialization (pure)
# --------------------------------------------------------------------------- #

def _merge(windows: List[Window]) -> List[Window]:
    """Sort + merge overlapping/adjacent windows into a disjoint ordered list."""
    out: List[Window] = []
    for start, end in sorted(windows):
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
            continue
        out.append((start, end))
    return out


def _weekly_candidates(spec: dict, gen_start: datetime,
                       span_end: datetime) -> List[Window]:
    """Every occurrence of one weekly day/time spec whose start falls in
    [gen_start's week, span_end] — unclamped; the caller filters + clamps."""
    sh, sm = _parse_hhmm(spec["start_time"], "start_time")
    eh, em = _parse_hhmm(spec["end_time"], "end_time")
    start_dow = int(spec["start_dow"])
    end_dow = int(spec["end_dow"])

    base = (gen_start - timedelta(days=7)).date()
    day = base + timedelta(days=(start_dow - base.weekday()) % 7)
    days_off = (end_dow - start_dow) % 7
    out: List[Window] = []
    while True:
        start = datetime(day.year, day.month, day.day, sh, sm)
        end_day = day + timedelta(days=days_off)
        end = datetime(end_day.year, end_day.month, end_day.day, eh, em)
        while end <= start:
            end += timedelta(days=1)
        if start > span_end:
            break
        out.append((start, end))
        day += timedelta(days=7)
        if len(out) > MAX_MATERIALIZED_WINDOWS * 4:
            raise ScheduleError("The schedule produces too many windows.")
    return out


def materialize(config: dict, starts_at: datetime,
                ends_at: datetime) -> List[Window]:
    """Compile a (validated) schedule config into the disjoint, ordered list
    of ``[start, end)`` scoring windows inside ``[starts_at, ends_at]``.
    Raises :class:`ScheduleError` when the inputs can't produce a sane list
    (no windows at all, or too many)."""
    if starts_at is None or ends_at is None:
        raise ScheduleError(
            "A recurring schedule needs both a start and an end date.")
    if ends_at <= starts_at:
        raise ScheduleError("The event ends before it starts.")
    rule = (config or {}).get("rule") or {}
    rtype = rule.get("type")
    raw: List[Window] = []

    if rtype == "weekly":
        ordinal = rule.get("month_ordinal")
        interval = int(rule.get("interval_weeks") or 1)
        # With a month ordinal we must see the WHOLE month's occurrences to
        # know which one is "first"/"last" — generate from the 1st of the
        # start month (an occurrence before starts_at simply clamps away).
        gen_start = (starts_at.replace(day=1, hour=0, minute=0, second=0,
                                       microsecond=0)
                     if ordinal else starts_at)
        for spec in rule.get("windows") or []:
            occs = _weekly_candidates(spec, gen_start, ends_at)
            if ordinal:
                by_month: dict = {}
                for occ in occs:
                    by_month.setdefault((occ[0].year, occ[0].month), []).append(occ)
                occs = []
                for _, month_occs in sorted(by_month.items()):
                    idx = ordinal - 1 if ordinal > 0 else -1
                    if -len(month_occs) <= idx < len(month_occs):
                        occs.append(month_occs[idx])
            elif interval > 1:
                # Anchor on the first occurrence that touches the event span.
                anchored = [o for o in occs if o[1] > starts_at]
                if anchored:
                    anchor_day = anchored[0][0].date()
                    occs = [o for o in anchored
                            if ((o[0].date() - anchor_day).days // 7) % interval == 0]
                else:
                    occs = []
            raw.extend(occs)

    elif rtype == "daily":
        sh, sm = _parse_hhmm(rule.get("start_time"), "start_time")
        eh, em = _parse_hhmm(rule.get("end_time"), "end_time")
        day = (starts_at - timedelta(days=1)).date()
        last = ends_at.date()
        while day <= last:
            start = datetime(day.year, day.month, day.day, sh, sm)
            end = datetime(day.year, day.month, day.day, eh, em)
            if end <= start:
                end += timedelta(days=1)
            raw.append((start, end))
            day += timedelta(days=1)

    elif rtype == "custom":
        for win in rule.get("windows") or []:
            raw.append((datetime.fromtimestamp(int(win["start"])),
                        datetime.fromtimestamp(int(win["end"]))))
    else:
        raise ScheduleError("Unknown schedule rule type.")

    clamped = []
    for start, end in raw:
        start = max(start, starts_at)
        end = min(end, ends_at)
        if end > start:
            clamped.append((start, end))
    windows = _merge(clamped)
    if not windows:
        raise ScheduleError(
            "This schedule never opens between the event's start and end "
            "dates — adjust the dates or the schedule.")
    if len(windows) > MAX_MATERIALIZED_WINDOWS:
        raise ScheduleError(
            f"This schedule produces {len(windows)} scoring windows (the "
            f"limit is {MAX_MATERIALIZED_WINDOWS}) — simplify it or shorten "
            "the event.")
    return windows


# --------------------------------------------------------------------------- #
# Read-side helpers (operate on materialized window lists)
# --------------------------------------------------------------------------- #

def in_any_window(windows, ts: datetime) -> bool:
    """Whether ``ts`` falls inside any ``[start, end)`` window. An EMPTY list
    means "no schedule" → always True (the overall event window still
    applies; only a present schedule narrows further)."""
    if not windows:
        return True
    return any(start <= ts < end for start, end in windows)


def current_window(windows, now: datetime) -> Optional[Window]:
    for start, end in windows or ():
        if start <= now < end:
            return (start, end)
    return None


def next_window(windows, now: datetime) -> Optional[Window]:
    """The first window that OPENS after ``now`` (exclusive of one already
    open — pair with :func:`current_window`)."""
    for start, end in windows or ():
        if start > now:
            return (start, end)
    return None


# --------------------------------------------------------------------------- #
# Human summary (Discord lines, event pages, audit diffs)
# --------------------------------------------------------------------------- #

def _dow_name(dow) -> str:
    try:
        return _DOW_NAMES[int(dow)]
    except (ValueError, TypeError, IndexError):
        return "?"


_ORDINAL_LABELS = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}


def apply_schedule(session, event, config: Optional[dict],
                   now: Optional[datetime] = None) -> List[Window]:
    """Store ``config`` on ``event`` and rewrite its ``web_event_windows``
    rows to match. Returns the materialized windows ([] when the schedule was
    cleared). Raises :class:`ScheduleError` for an unusable schedule; the
    caller owns the commit.

    Windows that have FULLY ELAPSED on a live event are preserved verbatim
    (``source='frozen'``) rather than regenerated: an admin tweaking the rule
    in week three must not retroactively move week one's goalposts — credit
    already earned there, and the loot figures derived from it, stay valid.
    Everything from ``now`` forward is regenerated from the new rule.
    """
    import json

    from db.models import EventWindow

    now = now or datetime.now()
    event.schedule_config = json.dumps(config) if config else None
    if not config:
        session.query(EventWindow).filter(
            EventWindow.event_id == event.id).delete(synchronize_session=False)
        return []

    windows = materialize(config, event.starts_at, event.ends_at)
    keep: List[Window] = []
    if (getattr(event, "status", None) or "draft") == "active":
        keep = [
            (w.starts_at, w.ends_at)
            for w in session.query(EventWindow)
            .filter(EventWindow.event_id == event.id,
                    EventWindow.ends_at <= now)
            .order_by(EventWindow.starts_at)
            .all()
        ]
    frozen = {(s, e) for s, e in keep}
    merged = _merge(keep + [w for w in windows if w[1] > now])

    session.query(EventWindow).filter(
        EventWindow.event_id == event.id).delete(synchronize_session=False)
    session.flush()
    for seq, (start, end) in enumerate(merged):
        session.add(EventWindow(
            event_id=event.id, seq=seq, starts_at=start, ends_at=end,
            source="frozen" if (start, end) in frozen else "rule",
        ))
    return merged


def load_windows(session, event_id) -> List[Window]:
    """An event's materialized scoring windows, chronological ([] when it has
    no schedule)."""
    from db.models import EventWindow

    return [
        (w.starts_at, w.ends_at)
        for w in session.query(EventWindow)
        .filter(EventWindow.event_id == event_id)
        .order_by(EventWindow.starts_at)
        .all()
    ]


def describe(config) -> Optional[str]:
    """One human-readable line for a schedule config ("Weekly: Sat 00:00 →
    Mon 00:00 UTC, every 2nd week"), or None when there is no schedule."""
    config = parse_config(config) if not isinstance(config, dict) else config
    if not config:
        return None
    rule = config.get("rule") or {}
    rtype = rule.get("type")
    if rtype == "weekly":
        spans = ", ".join(
            f"{_dow_name(s.get('start_dow'))} {s.get('start_time')} → "
            f"{_dow_name(s.get('end_dow'))} {s.get('end_time')}"
            for s in rule.get("windows") or []
        )
        ordinal = rule.get("month_ordinal")
        interval = int(rule.get("interval_weeks") or 1)
        if ordinal in _ORDINAL_LABELS:
            cadence = f", the {_ORDINAL_LABELS[ordinal]} occurrence each month"
        elif interval == 2:
            cadence = ", every other week"
        elif interval > 2:
            cadence = f", every {interval} weeks"
        else:
            cadence = ""
        return f"Weekly: {spans} UTC{cadence}"
    if rtype == "daily":
        return (f"Daily: {rule.get('start_time')} → {rule.get('end_time')} UTC")
    if rtype == "custom":
        n = len(rule.get("windows") or [])
        return f"{n} custom scoring window{'s' if n != 1 else ''}"
    return None
