"""Scheduling logic for the hourly WOM group-membership sync.

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
