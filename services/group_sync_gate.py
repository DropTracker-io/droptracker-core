"""Scheduling logic for the WOM group-membership sync.

The round still fires hourly, but since 2026-11-11 each round only syncs the
groups that are DUE: every 12 hours, or every 6 for a group on a paid tier
(see ``group_sync_due`` at the bottom). WiseOldMan asked us to cut request
volume and this sync was 59% of everything we sent them.

The "pure half" of ``bots.main.start_group_sync`` (same pattern as
``services/activity_launch_core.py`` and ``services/channel_name_render.py``):
no ``interactions`` import, no DB access, so the unit tests can drive it
directly instead of importing the whole Discord bot.

It exists because the scheduling around that sync had two defects that together
produced the 2026-09-03 "the bot re-added everyone / ~380 messages" report:

1. The due-check **stamped the Redis gate before returning True**, spending it
   on every *attempt* rather than on work actually happening; and
2. the round awaited a cosmetic voice-channel rename BEFORE the sync, unguarded.

Discord allows only ~2 renames per 10 minutes per channel and a second loop
renamed the same channel, so the bucket was permanently saturated and
interactions.py raised ``RuntimeError: Attempted to lock a bucket that is
already locked``. That exception aborted the round before the sync ran — with
the gate already spent, so the next hour was blocked too. It killed 20 of 43
attempts (47%) over three days and stretched the "hourly" sync to gaps of up to
7 hours. New clan members accumulated unassociated until a sync finally landed
and announced the entire backlog at once.

The invariant this module encodes: **nothing cosmetic may prevent the sync from
running, and the gate is spent only when a sync actually starts.**
"""
import os
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

GATE_KEY = "last_group_sync"
GATE_INTERVAL = timedelta(hours=1)


def is_sync_due(last_sync_raw: Optional[str], now: Optional[datetime] = None) -> bool:
    """Whether the membership sync is due, given the stored gate value.

    PURE — it never writes the gate. Claiming is a separate step so a failure
    between the two cannot silently consume an hour of syncing.

    A missing or unparseable gate reads as "due": an unreadable timestamp must
    not wedge the sync forever.
    """
    if not last_sync_raw:
        return True
    try:
        last_sync = datetime.fromisoformat(last_sync_raw)
    except (TypeError, ValueError):
        return True
    return (now or datetime.now()) - last_sync > GATE_INTERVAL


async def run_group_sync_round(
    *,
    is_due: Callable[[], bool],
    claim: Callable[[], None],
    sync: Callable[[], Awaitable[None]],
    refresh_label: Callable[[], Awaitable[None]],
    log: Callable[[str], None] = print,
) -> dict:
    """Run one scheduled round: maybe sync, then refresh the countdown label.

    Ordering and isolation are the whole point:

    * ``claim`` is called immediately after ``is_due`` with **no await between
      them**, so check-and-claim is atomic on the single-threaded event loop.
      It is claimed *before* the sync, not after: a full round is 239
      rate-limited WOM calls over many minutes, and two overlapping rounds
      would double the API spend and race each other's membership writes.
    * ``refresh_label`` is cosmetic, runs **last**, and its failure is
      swallowed. It must never be able to skip a sync.
    * ``sync``'s own failure is caught and logged rather than propagating —
      losing a round loudly is fine, losing it silently is what caused the bug.

    Returns {"synced": bool, "sync_failed": bool, "label_failed": bool}.
    """
    result = {"synced": False, "sync_failed": False, "label_failed": False}

    if is_due():
        claim()
        result["synced"] = True
        try:
            await sync()
        except Exception as e:
            result["sync_failed"] = True
            log(f"Group membership sync round failed: {e}")

    try:
        await refresh_label()
    except Exception as e:
        # Rate limits here are routine; the countdown label just goes stale.
        result["label_failed"] = True
        log(f"Couldn't update the WOM refresh channel name: {e}")

    return result


# --------------------------------------------------------------------------- #
# Per-group cadence
#
# The hourly round used to fetch every WOM-linked group every time (~350 groups
# x 16 rounds = ~5,500 calls a day). Each group now has its own clock, kept in
# two Redis hashes keyed by WOM group id (epoch seconds):
#
#   wom:member_sync:ok    last sync that got a roster back from WOM
#   wom:member_sync:fail  last attempt that did not
#
# Both are written by db.ops._sync_group_from_wom, so a manual "Sync from WOM"
# also resets the scheduled clock. The interval is read at check time, so a
# group that subscribes moves to the faster cadence on the next round.
# --------------------------------------------------------------------------- #
SYNC_OK_HASH = "wom:member_sync:ok"
SYNC_FAIL_HASH = "wom:member_sync:fail"

FREE_SYNC_SECONDS = int(float(os.getenv("WOM_MEMBER_SYNC_HOURS", "12")) * 3600)
PAID_SYNC_SECONDS = int(float(os.getenv("WOM_MEMBER_SYNC_PAID_HOURS", "6")) * 3600)
# A fetch that returned nothing (WOM error, empty or deleted group) is retried
# sooner than a full interval, but not every round.
FAILED_SYNC_RETRY_SECONDS = int(float(os.getenv("WOM_MEMBER_SYNC_RETRY_HOURS", "3")) * 3600)
# The round fires on a 60-minute tick, so a group stamped a few minutes into
# the previous round would otherwise slip a whole extra hour every cycle.
DUE_SLACK_SECONDS = 600


def sync_interval_seconds(is_paid: bool) -> int:
    return PAID_SYNC_SECONDS if is_paid else FREE_SYNC_SECONDS


def _epoch(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def group_sync_due(last_ok, last_fail, is_paid: bool, now: float) -> bool:
    """Whether one group's scheduled membership sync is due. PURE.

    A group never synced (or with an unreadable stamp) is due. After a failed
    attempt it waits FAILED_SYNC_RETRY_SECONDS, never longer than its interval.
    """
    interval = sync_interval_seconds(is_paid)
    ok, fail = _epoch(last_ok), _epoch(last_fail)
    if ok is not None and now - ok < interval - DUE_SLACK_SECONDS:
        return False
    if fail is not None and now - fail < min(FAILED_SYNC_RETRY_SECONDS, interval) - DUE_SLACK_SECONDS:
        return False
    return True


def due_wom_ids(wom_ids, paid_wom_ids, ok_stamps, fail_stamps, now: float) -> list:
    """The subset of ``wom_ids`` due now, in the order given. PURE.

    ``ok_stamps``/``fail_stamps`` are the two Redis hashes as read (keys and
    values may be bytes or str).
    """
    def _norm(stamps):
        out = {}
        for k, v in (stamps or {}).items():
            if isinstance(k, bytes):
                k = k.decode("utf-8", "ignore")
            out[str(k)] = v
        return out

    ok, fail = _norm(ok_stamps), _norm(fail_stamps)
    paid = {str(w) for w in (paid_wom_ids or ())}
    return [
        w for w in wom_ids
        if group_sync_due(ok.get(str(w)), fail.get(str(w)), str(w) in paid, now)
    ]
