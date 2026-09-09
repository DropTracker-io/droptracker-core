"""Integration scenario: the KC watermark lock must never block the event loop.

Run standalone (NOT collected by pytest — see test_kc_watermark_lock_db.py,
which invokes it as a subprocess so the unit-test sys.modules stubs never
apply):

    venv/bin/python tests/integration/kc_watermark_lock_it.py

WHAT THIS GUARDS. `record_kill_count` row-locks `player_npc_kc`, and the six
webhook_consumer "workers" are asyncio tasks sharing ONE event loop over a
*blocking* driver (pymysql). With a plain `FOR UPDATE`, a task waiting on the
lock parks the whole loop inside `socket.readinto` — including the task that
holds the lock and is the only one that could release it. On 2026-09-03 that
deadlock froze the consumer ~45 minutes out of every hour and held the queue at
a 34-minute backlog. `_lock_watermark_row` fixes it with NOWAIT + a cooperative
retry that `await`s (and so yields) between attempts.

The invariant that separates the two, and the only one worth asserting:

    THE LOCK HOLDER'S COMMIT TIME MUST NOT DEPEND ON THE WAITER.

A holder that means to hold the row for HOLD_S must commit at ~HOLD_S whether
or not somebody else is waiting on that row. Under the blocking version the
holder instead commits at `innodb_lock_wait_timeout` (measured: a 2.0s holder
committed at 15.31s) because it cannot get the loop back. That is a clean,
fast, unambiguous signal, and it fails if anyone ever changes the NOWAIT back.

The unit tests cannot cover this: they never construct a real session, so there
is no real transaction, no real row lock, and nothing to deadlock on.

Uses a synthetic negative (player_id, npc_id) — `player_npc_kc` has no foreign
keys — salted per run, and deletes it in a finally.
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from sqlalchemy.orm import sessionmaker  # noqa: E402

from data.submissions.kc_milestones import (  # noqa: E402
    KcWatermarkBusy,
    _lock_watermark_row,
    record_kill_count,
)
from db.models import PlayerNpcKc  # noqa: E402
from db.models.base import engine  # noqa: E402

Session = sessionmaker(bind=engine)

# Negative ids never collide with real players; salted so two runs (or a run
# racing a developer's own) cannot collide with each other either.
SALT = os.getpid() % 10000
PID = -(900000 + SALT)
NID = -(900000 + SALT)

HOLD_S = 2.0        # how long the holder means to keep the row
TOLERANCE_S = 1.5   # generous: a loaded prod box still finishes well inside this


def _seed(kc=100):
    s = Session()
    try:
        s.query(PlayerNpcKc).filter_by(player_id=PID, npc_id=NID).delete()
        s.add(PlayerNpcKc(player_id=PID, npc_id=NID, kill_count=kc))
        s.commit()
    finally:
        s.close()


def _cleanup():
    s = Session()
    try:
        s.query(PlayerNpcKc).filter_by(player_id=PID, npc_id=NID).delete()
        s.commit()
    finally:
        s.close()


async def _holder(result):
    s = Session()
    t0 = time.perf_counter()
    try:
        await _lock_watermark_row(s, PID, NID)
        # Stand in for pb.py's post-lock notification block: real work that
        # needs the event loop to come back before the transaction can commit.
        await asyncio.sleep(HOLD_S)
        s.commit()
        result["holder_commit_s"] = time.perf_counter() - t0
    except Exception as e:
        result["holder_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        s.rollback()
        s.close()


async def _waiter(result):
    await asyncio.sleep(0.3)  # let the holder take it first
    s = Session()
    t0 = time.perf_counter()
    try:
        await _lock_watermark_row(s, PID, NID)
        result["waiter_acquired_s"] = time.perf_counter() - t0
    except KcWatermarkBusy:
        result["waiter_busy"] = True
    except Exception as e:  # e.g. a blocking FOR UPDATE hitting 1205
        result["waiter_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        s.rollback()
        s.close()


async def test_holder_commit_is_independent_of_the_waiter():
    _seed()
    result = {}
    # return_exceptions so a failing task can never strand the other one
    # mid-transaction still holding the row — that turns a clean assertion
    # failure into a cascade of lock timeouts in the cleanup.
    outcomes = await asyncio.gather(_holder(result), _waiter(result),
                                    return_exceptions=True)
    for o in outcomes:
        if isinstance(o, BaseException):
            raise AssertionError(f"a task failed outright: {type(o).__name__}: {o}")

    assert not result.get("waiter_error"), (
        f"waiter errored instead of acquiring: {result.get('waiter_error')}\n"
        "A blocking FOR UPDATE raises 1205 here; the NOWAIT path retries instead."
    )
    assert not result.get("waiter_busy"), (
        "waiter exhausted its retry budget while the holder held the row for "
        f"only {HOLD_S}s — the budget is too small or the lock is not being released"
    )
    assert "waiter_acquired_s" in result, "waiter never acquired the lock"

    assert not result.get("holder_error"), f"holder errored: {result['holder_error']}"
    commit_s = result["holder_commit_s"]
    assert commit_s < HOLD_S + TOLERANCE_S, (
        f"HOLDER COMMITTED AT {commit_s:.2f}s BUT ONLY MEANT TO HOLD FOR {HOLD_S}s.\n"
        "The waiter delayed the holder — that is the event-loop deadlock this "
        "branch exists to prevent. Has the NOWAIT in _lock_watermark_row been "
        "changed back to a blocking FOR UPDATE?"
    )
    print(f"  holder committed @ {commit_s:.2f}s (meant to hold {HOLD_S}s) — "
          f"waiter acquired @ {result['waiter_acquired_s']:.2f}s")


async def test_contending_writers_serialize_without_losing_the_watermark():
    """Two concurrent record_kill_count calls for the same pair: the higher KC
    must win, and neither may deadlock."""
    _seed(kc=100)

    async def submit(kc, out):
        s = Session()
        try:
            out[kc] = await record_kill_count(s, PID, NID, kc)
            s.commit()
        finally:
            s.rollback()
            s.close()

    out = {}
    await asyncio.gather(submit(150, out), submit(175, out))

    s = Session()
    try:
        final = s.query(PlayerNpcKc).filter_by(player_id=PID, npc_id=NID).one()
        assert final.kill_count == 175, f"watermark went backwards: {final.kill_count}"
    finally:
        s.close()
    print(f"  concurrent 150 + 175 -> watermark 175, advanced="
          f"{[r.advanced for r in out.values()]}")


async def main():
    try:
        for fn in (test_holder_commit_is_independent_of_the_waiter,
                   test_contending_writers_serialize_without_losing_the_watermark):
            print(f"{fn.__name__}:")
            await fn()
        print("ALL KC WATERMARK LOCK INTEGRATION ASSERTIONS PASSED")
    finally:
        _cleanup()


if __name__ == "__main__":
    asyncio.run(main())
