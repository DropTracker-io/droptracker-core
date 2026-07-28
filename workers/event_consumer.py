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

# P1-1: reliable delivery. The main queue is drained with BRPOPLPUSH into an
# in-flight PROCESSING list so a crash/restart mid-apply doesn't lose the
# envelope (BRPOP removed it before apply = at-most-once). On success the entry
# is LREM'd; on a handled exception it is moved to a bounded DEAD list (poison
# messages don't loop); on startup any orphans left in PROCESSING (from a hard
# crash) are reclaimed to the queue. Re-apply is safe — the completion ledger's
# unique (task_id, team_id, submission_guid) makes it idempotent.
PROCESSING_KEY = "events:submissions:processing"
DEAD_KEY = "events:submissions:dead"
DEAD_MAX_ENTRIES = 1000

# P0-5: transient infrastructure errors (deadlock, lock-wait timeout, "MySQL
# server has gone away", pool exhaustion, Redis hiccup) are the COMMON failure
# at scale and must be retried, not dead-lettered — dead-lettering them
# silently discards legitimately-earned credit. Only genuine poison payloads
# (or envelopes that keep failing past the cap) go to the DEAD list.
RETRY_MAX_ATTEMPTS = 5
_ATTEMPTS_KEY = "_attempts"

# Batch 2: in-process apply lanes. Applies are DB-bound (SQL round trips, not
# CPU), so N concurrent lanes give ~Nx throughput on one core. Envelopes are
# routed by player_id so everything the watermark/baseline folds key on stays
# strictly ordered per player; cross-player writes to shared rows (team score,
# progress, bingo bonuses, board state) are serialized by the engine's FOR
# UPDATE locks. Lane count is sized against the DB pool (base 5): 4 lanes +
# the main loop's housekeeping session.
LANES = 4
LANE_QUEUE_MAX = 50   # bounded → a hot lane backpressures the dispatcher
QUEUE_ALARM_DEPTH = 10_000  # pending above this = lanes can't keep up


def _lane_for(entry_bytes) -> int:
    """Deterministic lane for an envelope (player_id modulo). Malformed
    payloads route to lane 0 — they're skip-logged during apply anyway."""
    try:
        envelope = json.loads(entry_bytes)
        return int(envelope.get("player_id") or 0) % LANES
    except Exception:
        return 0


def _is_retryable(exc) -> bool:
    """True for infrastructure errors worth retrying; False for poison."""
    try:
        from sqlalchemy.exc import (
            DBAPIError, InterfaceError, OperationalError,
            TimeoutError as SATimeoutError,
        )

        if isinstance(exc, (OperationalError, InterfaceError, SATimeoutError)):
            return True
        if isinstance(exc, DBAPIError) and getattr(
                exc, "connection_invalidated", False):
            return True
    except ImportError:
        pass
    if isinstance(exc, _redis.RedisError):
        return True
    return False


def _reclaim_inflight(r, queue_key: str) -> int:
    """Move any envelopes stranded in the PROCESSING list (a hard crash between
    BRPOPLPUSH and the success LREM) back onto the queue for one more attempt.
    Returns how many were reclaimed."""
    reclaimed = 0
    try:
        while r.rpoplpush(PROCESSING_KEY, queue_key) is not None:
            reclaimed += 1
            if reclaimed > 100_000:  # runaway backstop
                break
    except Exception:
        log.error("In-flight reclaim failed:\n%s", traceback.format_exc())
    if reclaimed:
        log.warning("Reclaimed %d in-flight event envelope(s) after restart", reclaimed)
    return reclaimed


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


def _reconcile_effort(state) -> int:
    """Sync `web_event_effort.frozen_at` with the freeze gate (Bingo EHB)."""
    from api.core import get_db_session, reset_db_connections
    from services import event_engine

    db_session = get_db_session()
    try:
        changed = event_engine.reconcile_effort_freezes(db_session, state)
        if changed:
            db_session.commit()
        return changed
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
        reset_db_connections()


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


def _end_won_event(r, event_id) -> None:
    """End a board-game event whose winner just crossed the finish (win.rule
    finish_tile). Reuses the normal lifecycle end (standings notification,
    guild-mirror teardown, Redis gate)."""
    from api.core import get_db_session, reset_db_connections
    from db.models import Event
    from services import event_lifecycle

    if not event_id:
        return
    db_session = get_db_session()
    try:
        ev = db_session.query(Event).filter(Event.id == event_id).first()
        if ev is not None and ev.status == "active":
            event_lifecycle.end_event(db_session, ev)
            db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
        reset_db_connections()


def _run_mercy_sweep(r) -> list:
    """Board-game anti-stall tick (web44a): auto-complete overdue tile tasks
    (zero coins) so an unobtainable drop can't freeze a team. Same cadence as
    the lifecycle sweep; commit-per-run."""
    from api.core import get_db_session, reset_db_connections
    from services import boardgame_engine

    db_session = get_db_session()
    try:
        swept = boardgame_engine.mercy_sweep(db_session, r)
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
        reset_db_connections()
    # W2: a mercy auto-roll can cross the finish. End those events (own session,
    # status-guarded) exactly like the completion-driven win path does.
    for entry in swept:
        if entry.get("won"):
            try:
                _end_won_event(r, entry.get("event_id"))
            except Exception:
                log.error("Auto-end after mercy board win failed:\n%s",
                          traceback.format_exc())
    return swept


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
    # P0-6: watermark/baseline/dedupe advances are staged and only flushed
    # once the ledger row is durable — a rolled-back apply leaves the
    # watermark untouched so the retry re-earns the same delta instead of
    # folding to 0 (silent credit loss).
    staged = event_engine.StagedWrites()
    try:
        results = event_engine.handle_envelope(
            db_session, r, state, envelope, staged=staged)
        db_session.commit()
        staged.flush(r)
        return results
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
        reset_db_connections()


async def _apply_entry(r, shared, entry_bytes) -> None:
    """Apply one claimed envelope end-to-end: process, classify failures
    (requeue transient / dead-letter poison — P0-5), handle board-win
    outcomes, and release the in-flight claim. Runs inside a lane."""
    try:
        outcomes = await asyncio.to_thread(
            _process_entry, r, shared["state"], entry_bytes)
    except Exception as exc:
        # P0-5: a transient infra error (deadlock / lock-wait / gone-away /
        # Redis blip) gets the envelope REQUEUED with a bounded attempt
        # counter — dead-lettering it would silently discard earned credit
        # for what is usually a self-healing failure. Only poison payloads
        # (or ones past the cap) go to DEAD.
        attempts, requeued = 0, False
        envelope = None
        try:
            envelope = json.loads(entry_bytes)
            attempts = int(envelope.get(_ATTEMPTS_KEY, 0))
        except Exception:
            envelope = None
        context = ""
        if isinstance(envelope, dict):
            context = " (type=%s player=%s guid=%s)" % (
                envelope.get("kind") or envelope.get("type"),
                envelope.get("player_id"),
                str(envelope.get("guid") or envelope.get(
                    "submission_guid"))[:40])
        if (isinstance(envelope, dict) and _is_retryable(exc)
                and attempts < RETRY_MAX_ATTEMPTS):
            try:
                from services.event_engine import QUEUE_KEY

                envelope[_ATTEMPTS_KEY] = attempts + 1
                requeue_bytes = json.dumps(envelope).encode()
                pipe = r.pipeline()
                pipe.lrem(PROCESSING_KEY, 1, entry_bytes)
                pipe.lpush(QUEUE_KEY, requeue_bytes)
                await asyncio.to_thread(pipe.execute)
                requeued = True
                log.warning(
                    "Transient %s applying envelope%s; requeued "
                    "(attempt %d/%d)", exc.__class__.__name__,
                    context, attempts + 1, RETRY_MAX_ATTEMPTS)
            except Exception:
                log.error("Requeue failed; falling back to "
                          "dead-letter:\n%s", traceback.format_exc())
        if not requeued:
            log.error("Failed to apply envelope%s; dead-lettering "
                      "(attempts=%d):\n%s", context, attempts,
                      traceback.format_exc())
            try:
                pipe = r.pipeline()
                pipe.lrem(PROCESSING_KEY, 1, entry_bytes)
                pipe.lpush(DEAD_KEY, entry_bytes)
                pipe.ltrim(DEAD_KEY, 0, DEAD_MAX_ENTRIES - 1)
                await asyncio.to_thread(pipe.execute)
            except Exception:
                log.error("Dead-letter move failed:\n%s",
                          traceback.format_exc())
        return
    for outcome in outcomes:
        log.info("Applied %s: event=%s task=%s team=%s",
                 outcome.get("kind"), outcome.get("event_id"),
                 outcome.get("task_id"), outcome.get("team_id"))
        # Board game (web44a, win.rule=finish_tile): the first team to cross
        # the finish ends the event — final standings post through the
        # normal event_ended flow.
        board = outcome.get("board") or {}
        roll = board.get("roll") or {}
        if roll.get("won"):
            # (The winning roll already published an admin bump, so the
            # state refreshes on the next loop pass.)
            try:
                await asyncio.to_thread(
                    _end_won_event, r, outcome.get("event_id"))
            except Exception:
                log.error("Auto-end after board win failed:\n%s",
                          traceback.format_exc())
    # Applied (or a no-op) — drop it from the in-flight list.
    await asyncio.to_thread(r.lrem, PROCESSING_KEY, 1, entry_bytes)


async def _lane_worker(lane_idx: int, r, shared, lane_queue) -> None:
    """One apply lane: serially applies every envelope routed to it. The
    backstop except keeps a lane alive on truly unexpected failures —
    _apply_entry already classifies and records everything expected."""
    while True:
        entry_bytes = await lane_queue.get()
        try:
            await _apply_entry(r, shared, entry_bytes)
        except Exception:
            log.error("Lane %d: unexpected apply failure:\n%s",
                      lane_idx, traceback.format_exc())
        finally:
            lane_queue.task_done()


async def _drain_queue_inline(r, lane_queues) -> int:
    """Drain events:submissions (+ the WOM lane) ahead of an event end — the
    queued envelopes must be applied while the ending event is still in the
    matcher state. Entries are dispatched through the normal lanes (per-player
    ordering holds) and the join() barrier guarantees everything has been
    applied before the caller lets the lifecycle sweep end the event."""
    from services.event_engine import QUEUE_KEY, WOM_QUEUE_KEY

    drained = 0
    while drained < FINAL_DRAIN_MAX_ENTRIES:
        entry = await asyncio.to_thread(r.rpoplpush, QUEUE_KEY, PROCESSING_KEY)
        if entry is None:
            entry = await asyncio.to_thread(
                r.rpoplpush, WOM_QUEUE_KEY, PROCESSING_KEY)
        if entry is None:
            break
        await lane_queues[_lane_for(entry)].put(entry)
        drained += 1
    for lane_queue in lane_queues:
        await lane_queue.join()
    return drained


async def _run_wom_final_passes(r, shared, lane_queues) -> bool:
    """Final WOM reconcile + inline drain for events whose window closed but
    which the lifecycle sweep hasn't ended yet. Returns True if any ran."""
    from services import event_wom_reconciler as wom

    state = shared["state"]
    event_ids = wom.pending_final_event_ids(state, r)
    if not event_ids:
        return False
    for event_id in event_ids:
        stats = await wom.final_reconcile(state, r, event_id)
        log.info("WOM final reconcile for event %s: %s", event_id, stats)
    drained = await _drain_queue_inline(r, lane_queues)
    if drained:
        log.info("Drained %d queue entries ahead of event end", drained)
    return True


async def run_consumer() -> None:
    from services.event_engine import (ADMIN_BUMP_CHANNEL, QUEUE_KEY,
                                       WOM_QUEUE_KEY)
    from services import event_wom_reconciler as wom

    log.info("Event consumer starting (queue=%s)", QUEUE_KEY)
    r = await asyncio.to_thread(_get_redis)
    # Recover envelopes left in-flight by a previous crash/restart (P1-1).
    await asyncio.to_thread(_reclaim_inflight, r, QUEUE_KEY)
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
    last_dead_count = 0

    # Batch 2: apply lanes. The main loop only claims + routes; the lanes do
    # the DB work concurrently (player-hash routing keeps per-player order).
    shared = {"state": None}
    lane_queues = [asyncio.Queue(maxsize=LANE_QUEUE_MAX) for _ in range(LANES)]
    lane_tasks = [  # noqa: F841 — referenced to keep the workers alive
        asyncio.create_task(_lane_worker(i, r, shared, lane_queues[i]))
        for i in range(LANES)
    ]
    log.info("Apply lanes started: %d (player-hash routed)", LANES)

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
                        await _run_wom_final_passes(r, shared, lane_queues)
                    except Exception:
                        log.error("WOM final pass failed:\n%s", traceback.format_exc())
                # P0-1: the sweep has per-event guards inside, but an
                # infra-level failure (the due-list query itself) must not
                # kill the consumer loop — and must not retry hot: stamp
                # last_sweep either way so the next attempt waits a full tick.
                try:
                    summary = await asyncio.to_thread(_run_lifecycle_sweep, r)
                except Exception:
                    summary = {}
                    log.error("Lifecycle sweep failed:\n%s", traceback.format_exc())
                last_sweep = time.time()
                if summary.get("activated") or summary.get("ended") or summary.get("failed"):
                    log.info("Lifecycle sweep: activated=%s ended=%s failed=%s",
                             summary.get("activated"), summary.get("ended"),
                             summary.get("failed"))
                    bumped = True  # transitions changed the active set
                # P0-5 visibility: queue / in-flight / dead depths once per
                # tick (~60s) — the one heartbeat line an operator needs to
                # tell a degraded consumer from a healthy one. DLQ growth is
                # an ERROR so it reaches Sentry the moment credit is dropped.
                try:
                    pending, wom_pending, inflight, dead = await asyncio.to_thread(
                        lambda: (r.llen(QUEUE_KEY), r.llen(WOM_QUEUE_KEY),
                                 r.llen(PROCESSING_KEY), r.llen(DEAD_KEY)))
                    lane_fill = sum(q.qsize() for q in lane_queues)
                    log.info(
                        "Queue depth: pending=%d wom=%d inflight=%d lanes=%d dead=%d",
                        pending, wom_pending, inflight, lane_fill, dead)
                    if pending > QUEUE_ALARM_DEPTH:
                        # Backpressure alarm (batch 2): the lanes cannot keep
                        # up — reaches Sentry so someone looks BEFORE credit
                        # lags into hours.
                        log.error(
                            "Event queue backlog: %d pending — apply lanes "
                            "can't keep up (investigate DB latency / raise "
                            "LANES / add capacity)", pending)
                    if dead > last_dead_count:
                        log.error(
                            "Event DLQ grew: %d -> %d entries "
                            "(events:submissions:dead) — dropped credit needs "
                            "an operator (inspect + requeue or purge)",
                            last_dead_count, dead)
                    last_dead_count = dead
                except Exception:
                    pass
                # Board-game mercy rule (web44a): overdue tile tasks
                # auto-complete so RNG can't strand a team.
                try:
                    swept = await asyncio.to_thread(_run_mercy_sweep, r)
                    if swept:
                        log.info("Mercy sweep released %d team(s): %s",
                                 len(swept), swept)
                        bumped = True  # new instance tasks need a state reload
                except Exception:
                    log.error("Mercy sweep failed:\n%s", traceback.format_exc())

            if state is None or bumped or (time.time() - last_refresh) >= STATE_REFRESH_SECONDS:
                state = await asyncio.to_thread(_refresh_state, r)
                shared["state"] = state  # lanes read the freshest snapshot
                last_refresh = time.time()
                log.info(
                    "Matcher state refreshed: %d active event(s), %d participant(s)%s",
                    len(state.events), len(state.participants),
                    " (admin bump)" if bumped else "",
                )
                # Bingo EHB: catch the `frozen_at` reporting flag up to the
                # freeze gate (accrual already stopped at completion time).
                try:
                    frozen = await asyncio.to_thread(_reconcile_effort, state)
                    if frozen:
                        log.info("Effort freeze reconcile updated %d row(s)", frozen)
                except Exception:
                    log.error("Effort freeze reconcile failed:\n%s",
                              traceback.format_exc())
                # Keep the shared EHB rate table warm. The read paths (web API,
                # engine) are synchronous and only ever read the cache, so
                # something async has to refresh it; this is a memo hit on all
                # but roughly one call a week.
                try:
                    from utils.wiseoldman import get_ehb_rates

                    await get_ehb_rates()
                except Exception:
                    log.warning("EHB rate refresh failed:\n%s", traceback.format_exc())

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

            # P1-1: atomically move the envelope to the in-flight PROCESSING
            # list instead of removing it outright, so a crash mid-apply keeps
            # it for reclaim rather than dropping credit. This loop only
            # claims and routes; the lanes do the applying (batch 2).
            # Priority: main queue first (non-blocking), then one WOM
            # envelope, then a blocking wait on the main queue — live plugin
            # drops always cut ahead of a reconcile burst.
            entry_bytes = await asyncio.to_thread(
                r.rpoplpush, QUEUE_KEY, PROCESSING_KEY)
            if entry_bytes is None:
                entry_bytes = await asyncio.to_thread(
                    r.rpoplpush, WOM_QUEUE_KEY, PROCESSING_KEY)
            if entry_bytes is None:
                entry_bytes = await asyncio.to_thread(
                    r.brpoplpush, QUEUE_KEY, PROCESSING_KEY, BRPOP_TIMEOUT)
            if entry_bytes is None:
                continue
            await lane_queues[_lane_for(entry_bytes)].put(entry_bytes)
        except Exception:
            log.error("Error in event consumer loop:\n%s", traceback.format_exc())
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run_consumer())
