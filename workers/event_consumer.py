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

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)
log = logging.getLogger("event_consumer")

BRPOP_TIMEOUT = 5
STATE_REFRESH_SECONDS = 30
LIFECYCLE_SWEEP_SECONDS = 60
_REDIS_PW = os.getenv("DB_PASS")


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


async def run_consumer() -> None:
    from services.event_engine import ADMIN_BUMP_CHANNEL, QUEUE_KEY

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

            # Lifecycle sweep (Task 21): scheduled activations / ends.
            if (time.time() - last_sweep) >= LIFECYCLE_SWEEP_SECONDS:
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
