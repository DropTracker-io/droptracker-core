"""Event completion engine consumer (backend Task 17).

Consumes processed-submission envelopes from the ``events:submissions`` Redis
list (LPUSH'd by the submission processors, gated on ``events:active``) and
turns them into task progress / completions / team points / bingo cells /
realtime frames / notification-queue rows via ``services/event_engine.py``.

Follows the ``workers/webhook_consumer.py`` pattern: async loop, BRPOP with a
short timeout, per-item DB session, try/except/finally. Launched by
``real_startup.sh`` as screen ``DT-events``.

Matcher state is refreshed every 30s, and immediately when web_api publishes a
bump on the ``rt:event-admin`` pubsub channel (event/task/roster mutations).
The worker also maintains the ``events:active`` gate set so the producer hooks
stay zero-overhead when no events run. Replays are safe: the completions
ledger is idempotent on (task, team, submission_guid).

Task 21 adds a ~60s lifecycle sweep (``services.event_lifecycle`` — the exact
functions the activate/end routes use): scheduled drafts whose ``starts_at``
passed are activated (validation failures notify the event's admin channel
once, then skip), and active events whose ``ends_at`` passed are ended.

WOM reconciliation (``services.event_wom_reconciler``) also lives in this
loop: a periodic pass turns WiseOldMan bulk gains into synthetic envelopes on
the same queue, key-moment freshness updates fire at activation / pre-end,
and events whose window just closed get a final WOM pass + inline queue drain
*before* the lifecycle sweep ends them (ended events drop out of the matcher
state, so late envelopes would otherwise be silently ignored).
"""
import asyncio
import json
import logging
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis as _redis
from dotenv import load_dotenv

load_dotenv()

from utils.sentry import init_sentry

init_sentry("droptracker-events")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)
log = logging.getLogger("event_consumer")

BRPOP_TIMEOUT = 5
STATE_REFRESH_SECONDS = 30
LIFECYCLE_SWEEP_SECONDS = 60
_REDIS_PW = os.getenv("DB_PASS")
# Cap for the pre-end inline drain — a runaway queue must not stall the loop.
FINAL_DRAIN_MAX_ENTRIES = 5000


def _get_redis():
    return _redis.Redis(host="127.0.0.1", port=6379, db=0, password=_REDIS_PW)


def _refresh_state(r):
    """Reload matcher state from MySQL and sync the events:active gate set."""
    from api.core import get_db_session, reset_db_connections
    from services import event_engine

    db_session = get_db_session()
    try:
        state = event_engine.load_matcher_state(db_session)
    finally:
        db_session.close()
        reset_db_connections()
    event_engine.set_active_events(r, list(state.events.keys()))
    return state


def _run_lifecycle_sweep(r) -> dict:
    """One scheduler tick (Task 21): activate due drafts / end due actives
    through the same service functions the web_api routes use."""
    from api.core import get_db_session, reset_db_connections
    from services import event_lifecycle

    db_session = get_db_session()
    try:
        return event_lifecycle.run_lifecycle_sweep(db_session, r)
    finally:
        db_session.close()
        reset_db_connections()


def _process_entry(r, state, entry_bytes) -> list:
    from api.core import get_db_session, reset_db_connections
    from services import event_engine

    try:
        envelope = json.loads(entry_bytes)
    except Exception:
        log.warning("Skipping malformed queue entry: %r", entry_bytes[:200])
        return []
    if not isinstance(envelope, dict) or envelope.get("v") != 1:
        log.warning("Skipping unsupported envelope: %r", str(envelope)[:200])
        return []

    db_session = get_db_session()
    try:
        results = event_engine.handle_envelope(db_session, r, state, envelope)
        db_session.commit()
        return results
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
        reset_db_connections()


async def _drain_queue_inline(r, state) -> int:
    """Synchronously drain events:submissions (final WOM pass ordering: the
    queued envelopes must be applied while the ending event is still in the
    matcher state)."""
    from services.event_engine import QUEUE_KEY

    drained = 0
    while drained < FINAL_DRAIN_MAX_ENTRIES:
        entry = await asyncio.to_thread(r.rpop, QUEUE_KEY)
        if entry is None:
            break
        await asyncio.to_thread(_process_entry, r, state, entry)
        drained += 1
    return drained


async def _run_wom_final_passes(r, state) -> bool:
    """Final WOM reconcile + inline drain for events whose window closed but
    which the lifecycle sweep hasn't ended yet. Returns True if any ran."""
    from services import event_wom_reconciler as wom

    event_ids = wom.pending_final_event_ids(state, r)
    if not event_ids:
        return False
    for event_id in event_ids:
        stats = await wom.final_reconcile(state, r, event_id)
        log.info("WOM final reconcile for event %s: %s", event_id, stats)
    drained = await _drain_queue_inline(r, state)
    if drained:
        log.info("Drained %d queue entries ahead of event end", drained)
    return True


async def run_consumer() -> None:
    from services.event_engine import ADMIN_BUMP_CHANNEL, QUEUE_KEY
    from services import event_wom_reconciler as wom

    log.info("Event consumer starting (queue=%s)", QUEUE_KEY)
    r = await asyncio.to_thread(_get_redis)
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    try:
        pubsub.subscribe(ADMIN_BUMP_CHANNEL)
    except Exception:
        log.warning("Could not subscribe to %s; relying on 30s refresh only",
                    ADMIN_BUMP_CHANNEL)
        pubsub = None

    state = None
    last_refresh = 0.0
    last_sweep = 0.0
    last_reconcile = 0.0
    reconcile_task = None

    async def _run_reconcile(current_state):
        try:
            stats = await wom.reconcile_once(current_state, r)
            if stats.get("targets"):
                log.info("WOM reconcile: %s", stats)
        except Exception:
            log.error("WOM reconcile failed:\n%s", traceback.format_exc())

    while True:
        try:
            # Drain any admin bumps (non-blocking) to force a refresh.
            bumped = False
            if pubsub is not None:
                try:
                    while True:
                        message = pubsub.get_message(timeout=0)
                        if message is None:
                            break
                        if message.get("type") == "message":
                            bumped = True
                except Exception:
                    pass

            # Lifecycle sweep (Task 21): scheduled activations / ends. Events
            # about to be ended get their final WOM pass (and a queue drain)
            # first, while they're still in the matcher state.
            if (time.time() - last_sweep) >= LIFECYCLE_SWEEP_SECONDS:
                if state is not None:
                    try:
                        await _run_wom_final_passes(r, state)
                    except Exception:
                        log.error("WOM final pass failed:\n%s", traceback.format_exc())
                summary = await asyncio.to_thread(_run_lifecycle_sweep, r)
                last_sweep = time.time()
                if summary.get("activated") or summary.get("ended") or summary.get("failed"):
                    log.info("Lifecycle sweep: activated=%s ended=%s failed=%s",
                             summary.get("activated"), summary.get("ended"),
                             summary.get("failed"))
                    bumped = True  # transitions changed the active set

            if state is None or bumped or (time.time() - last_refresh) >= STATE_REFRESH_SECONDS:
                state = await asyncio.to_thread(_refresh_state, r)
                last_refresh = time.time()
                log.info(
                    "Matcher state refreshed: %d active event(s), %d participant(s)%s",
                    len(state.events), len(state.participants),
                    " (admin bump)" if bumped else "",
                )

            # WOM reconciliation runs as a BACKGROUND task: the update
            # rotation inside it awaits one rate-limiter slot per player
            # (up to ~a minute per cycle), and queue draining must not stall
            # behind it. Skip a cycle rather than overlap two passes.
            if (time.time() - last_reconcile) >= wom.WOM_RECONCILE_SECONDS:
                last_reconcile = time.time()
                if reconcile_task is None or reconcile_task.done():
                    reconcile_task = asyncio.create_task(_run_reconcile(state))
                else:
                    log.warning("WOM reconcile still running; skipping this cycle")
            try:
                await wom.run_key_moment_updates(state, r)
            except Exception:
                log.error("WOM key-moment pass failed:\n%s", traceback.format_exc())

            result = await asyncio.to_thread(r.brpop, QUEUE_KEY, BRPOP_TIMEOUT)
            if result is None:
                continue
            _, entry_bytes = result
            outcomes = await asyncio.to_thread(_process_entry, r, state, entry_bytes)
            for outcome in outcomes:
                log.info("Applied %s: event=%s task=%s team=%s",
                         outcome.get("kind"), outcome.get("event_id"),
                         outcome.get("task_id"), outcome.get("team_id"))
        except Exception:
            log.error("Error in event consumer loop:\n%s", traceback.format_exc())
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run_consumer())
