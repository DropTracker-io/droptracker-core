# utils/event_end_hold.py — hold a scheduled event end while intake recovers.
#
# t274. An event's scheduled end is final: end_event freezes standings, pays
# clan points and announces the winner, and nothing scores a past event. After
# downtime that used to happen before the outage's submissions were replayed:
# the events worker's first lifecycle sweep ran the moment it booted, minutes
# ahead of the R2 drain and the webhook catch-up. A plain queue backlog at an
# event's end (2026-08-01 ran ~107 min behind) lost credit the same way.
#
# So the sweep asks, for each event due to end: is a recovery in flight that
# could still deliver a submission received before this event's end? If so it
# skips the end this tick and asks again next tick, up to a cap. Holding is
# safe because envelopes carry the receive time (t274 phase 1): a held event
# cannot score anything received after its end.
#
# Every signal fails open. A Redis error or a missing key means "no hold",
# which is the old behaviour. Stdlib only (like utils.event_window): the
# lifecycle, the events worker, the R2 drain and the webhook bot all touch
# these keys, and the unit-test conftest stubs the whole ``services`` package.

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

# ── Redis keys ────────────────────────────────────────────────────────────────
# Written by the events worker every lifecycle tick (epoch seconds).
HEARTBEAT_KEY = "events:consumer:heartbeat"
HEARTBEAT_TTL_SECONDS = 7 * 24 * 3600
# Set by the events worker at boot when the heartbeat shows it was down; the
# value is the last heartbeat before the gap (epoch seconds).
BOOT_RECOVERY_KEY = "events:recovery:boot"
# A restart for a deploy is seconds; anything longer than this was an outage.
BOOT_GAP_SECONDS = 5 * 60
# Long enough for the R2 drain (OnBootSec=3min) and the webhook bot's catch-up
# to raise their own markers, which then take over.
BOOT_RECOVERY_TTL_SECONDS = 15 * 60
# Set by scripts/drain_r2_spool.py while the spool holds real captures; the
# value is the oldest capture's ISO time. The TTL is a backstop for a drain
# that stopped running: it outlasts one 25-minute time-boxed pass.
R2_RECOVERY_KEY = "intake:recovery:r2"
R2_RECOVERY_TTL_SECONDS = 45 * 60
# Set by services/webhook_catchup.py while a catch-up runs; the value is the
# snowflake the replay starts from. Refreshed as it works, so a crashed
# catch-up stops holding events within the TTL.
CATCHUP_ACTIVE_KEY = "webhookbot:catchup_active"
CATCHUP_ACTIVE_TTL_SECONDS = 10 * 60
# Queues whose oldest entry tells us how far behind processing is.
INTAKE_QUEUE_KEY = "webhook:queue"
EVENT_QUEUE_KEY = "events:submissions"
EVENT_PROCESSING_KEY = "events:submissions:processing"
# Per-event "this end is being held" marker (first hold, epoch seconds).
HOLD_MARKER_KEY = "events:{event_id}:endhold"

HOLD_MAX_SECONDS_DEFAULT = 2 * 3600
_DISCORD_EPOCH_MS = 1420070400000


def hold_max_seconds() -> int:
    """``EVENT_END_HOLD_MAX_SECONDS`` (default 2 h). ``0`` turns holding off."""
    raw = (os.getenv("EVENT_END_HOLD_MAX_SECONDS") or "").strip()
    if not raw:
        return HOLD_MAX_SECONDS_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return HOLD_MAX_SECONDS_DEFAULT


@dataclass(frozen=True)
class Signal:
    """One reason intake may still deliver old submissions.

    ``since`` is the earliest receive time the recovery may still deliver
    (naive UTC). ``None`` means unknown, which counts as relevant to every
    event."""
    reason: str
    since: Optional[datetime] = None


@dataclass
class Decision:
    hold: bool = False
    capped: bool = False
    reasons: list = field(default_factory=list)


REASON_TEXT = {
    "boot_gap": "the events service was offline",
    "r2_spool": "submissions captured during an outage are still being replayed",
    "webhook_catchup": "missed Discord webhook messages are still being replayed",
    "intake_backlog": "the submission queue is still catching up",
    "event_backlog": "the event queue is still catching up",
    "disk_spool": "submissions spooled to disk are waiting to be queued",
}


def decide(ends_at: Optional[datetime], now: datetime,
           signals: Iterable[Signal], max_seconds: int) -> Decision:
    """Whether a scheduled end due at ``ends_at`` should wait this tick.

    Pure. A signal is relevant when its recovery could still deliver something
    received at or before ``ends_at``. ``capped`` means a relevant recovery is
    still running but the hold has reached ``max_seconds``: end anyway, and
    tell someone."""
    if ends_at is None or max_seconds <= 0:
        return Decision()
    relevant = [s for s in signals if s.since is None or s.since <= ends_at]
    if not relevant:
        return Decision()
    reasons = sorted({s.reason for s in relevant})
    if (now - ends_at).total_seconds() >= max_seconds:
        return Decision(hold=False, capped=True, reasons=reasons)
    return Decision(hold=True, reasons=reasons)


def describe(reasons) -> str:
    return "; ".join(REASON_TEXT.get(r, r) for r in reasons) or "recovery in progress"


# ── Parsing helpers (never raise) ─────────────────────────────────────────────

def _text(raw) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode()
        except Exception:
            return None
    return str(raw).strip() or None


def _from_epoch(raw) -> Optional[datetime]:
    text = _text(raw)
    if text is None:
        return None
    try:
        return datetime.fromtimestamp(float(text))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _from_iso(raw) -> Optional[datetime]:
    text = _text(raw)
    if text is None:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        stamped = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if stamped.tzinfo is not None:
        stamped = stamped.astimezone(timezone.utc).replace(tzinfo=None)
    return stamped


def _from_snowflake(raw) -> Optional[datetime]:
    text = _text(raw)
    if text is None:
        return None
    try:
        ms = (int(text) >> 22) + _DISCORD_EPOCH_MS
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _entry_time(raw, field_name: str, parse) -> Optional[datetime]:
    text = _text(raw)
    if text is None:
        return None
    try:
        entry = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    return parse(entry.get(field_name))


def _oldest_in(redis_conn, keys, field_name: str, parse) -> tuple:
    """(found_anything, oldest receive time) across both ends of each list.
    An entry whose time can't be read still counts as a backlog (since=None)."""
    found, oldest, unknown = False, None, False
    for key in keys:
        for idx in (-1, 0):
            raw = redis_conn.lindex(key, idx)
            if raw is None:
                continue
            found = True
            when = _entry_time(raw, field_name, parse)
            if when is None:
                unknown = True
            elif oldest is None or when < oldest:
                oldest = when
    return found, (None if unknown else oldest)


# ── Signal gathering (Redis reads; each one fails open on its own) ────────────

def gather_signals(redis_conn, spool_count: Optional[Callable[[], int]] = None) -> list:
    """Every recovery signal currently raised. Errors drop that one signal."""
    if redis_conn is None:
        return []
    out = []

    def _try(fn):
        try:
            sig = fn()
            if sig is not None:
                out.append(sig)
        except Exception:
            pass

    def boot():
        raw = redis_conn.get(BOOT_RECOVERY_KEY)
        return Signal("boot_gap", _from_epoch(raw)) if raw is not None else None

    def r2():
        raw = redis_conn.get(R2_RECOVERY_KEY)
        return Signal("r2_spool", _from_iso(raw)) if raw is not None else None

    def catchup():
        raw = redis_conn.get(CATCHUP_ACTIVE_KEY)
        return Signal("webhook_catchup", _from_snowflake(raw)) if raw is not None else None

    def intake():
        found, oldest = _oldest_in(redis_conn, (INTAKE_QUEUE_KEY,), "enqueued_at", _from_iso)
        return Signal("intake_backlog", oldest) if found else None

    def events():
        found, oldest = _oldest_in(redis_conn, (EVENT_QUEUE_KEY, EVENT_PROCESSING_KEY),
                                   "ts", _from_epoch)
        return Signal("event_backlog", oldest) if found else None

    def disk():
        if spool_count is None:
            return None
        return Signal("disk_spool", None) if int(spool_count() or 0) > 0 else None

    for fn in (boot, r2, catchup, intake, events, disk):
        _try(fn)
    return out


# ── Writers used by the worker / drain / catch-up (best-effort) ───────────────

def write_heartbeat(redis_conn, now_epoch: float) -> None:
    try:
        redis_conn.set(HEARTBEAT_KEY, int(now_epoch), ex=HEARTBEAT_TTL_SECONDS)
    except Exception:
        pass


def note_boot(redis_conn, now_epoch: float) -> Optional[int]:
    """At events-worker start: if the last heartbeat is older than
    :data:`BOOT_GAP_SECONDS`, raise the boot marker (value = that heartbeat)
    and return the gap in seconds. No heartbeat at all (first deploy, or a
    gap past its 7-day TTL) raises nothing."""
    try:
        raw = redis_conn.get(HEARTBEAT_KEY)
        last = float(_text(raw)) if raw is not None else None
    except Exception:
        return None
    if last is None:
        return None
    gap = now_epoch - last
    if gap <= BOOT_GAP_SECONDS:
        return None
    try:
        redis_conn.set(BOOT_RECOVERY_KEY, int(last), ex=BOOT_RECOVERY_TTL_SECONDS)
    except Exception:
        return None
    return int(gap)


# Raised once per outage by whoever notices the events worker went quiet.
STALL_ALERTED_KEY = "events:consumer:stall-alerted"
STALL_ALERT_AFTER_SECONDS = 5 * 60


def heartbeat_age(redis_conn, now_epoch: float) -> Optional[int]:
    """Seconds since the events worker's last lifecycle tick, or None when it
    has never written one (or Redis can't say)."""
    try:
        raw = redis_conn.get(HEARTBEAT_KEY)
        if raw is None:
            return None
        return max(0, int(now_epoch - float(_text(raw))))
    except Exception:
        return None


def check_events_worker(redis_conn, now_epoch: float) -> Optional[int]:
    """For another long-running process's maintenance loop (t275): the age
    of a stalled events worker the FIRST time it is seen stalled, else None.
    Re-arms once the heartbeat is fresh again."""
    age = heartbeat_age(redis_conn, now_epoch)
    if age is None:
        return None
    try:
        if age < STALL_ALERT_AFTER_SECONDS:
            redis_conn.delete(STALL_ALERTED_KEY)
            return None
        if redis_conn.set(STALL_ALERTED_KEY, int(now_epoch), nx=True,
                          ex=HEARTBEAT_TTL_SECONDS):
            return age
    except Exception:
        pass
    return None


def boot_gap_since(redis_conn) -> Optional[datetime]:
    """Start of the events worker's current boot gap, or None."""
    try:
        return _from_epoch(redis_conn.get(BOOT_RECOVERY_KEY))
    except Exception:
        return None
