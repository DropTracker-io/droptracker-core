"""Deferred gear renders: a Redis queue drained by the intake API's workers.

A gear render is background work (the picture decorates later notifications)
and arrives in bursts: on 2026-10-20 a game-update login wave asked for far
more renders per minute than the box can draw. Rendering each one inline from
its upload meant a burst either spawned browsers without bound (the API ran out
of threads) or, once capacity was capped, dropped renders. A dropped render
costs the outfit its picture until the player changes gear and comes back.

So uploads only enqueue, and a few drainers per worker render at whatever rate
:mod:`services.page_screenshot`'s caps allow:

* ``gear:render:queue`` is a sorted set of ``"{player_id}:{fingerprint}"``
  scored by enqueue time, so a repeat upload of the same outfit is one entry.
* Newest first: under a backlog the outfit a player is wearing *now* is the one
  their next personal best will show.
* Entries older than ``MAX_AGE_SECONDS`` are dropped unrendered, and the set is
  trimmed to ``MAX_LEN`` oldest-first, so a backlog that cannot drain stays
  bounded. An outfit dropped here is re-rendered when it is worn again.
* A render that cannot get capacity goes back with its original score.
* No Redis: :func:`enqueue` renders inline instead (the old behaviour).
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

QUEUE_KEY = "gear:render:queue"
MAX_AGE_SECONDS = 24 * 3600
MAX_LEN = 50_000
#: Drainers per process. page_screenshot's per-process cap is the real limit;
#: matching it keeps every slot busy without piling up waiters.
DRAINERS = 2
#: How long one drainer waits for capacity before putting the job back.
_SLOT_WAIT_SECONDS = 20.0
_IDLE_POLL_SECONDS = 1.0
_BUSY_BACKOFF_SECONDS = 2.0


def _redis():
    try:
        from utils.redis import RedisClient

        return RedisClient().client
    except Exception:
        return None


def _member(player_id: int, fingerprint: str) -> str:
    return f"{int(player_id)}:{fingerprint}"


def _parse(member) -> Optional[tuple[int, str]]:
    if isinstance(member, bytes):
        member = member.decode("utf-8", "replace")
    pid, sep, fp = str(member).partition(":")
    if not sep or not fp:
        return None
    try:
        return int(pid), fp
    except ValueError:
        return None


async def enqueue(player_id: int, fingerprint: str) -> None:
    """Queue a render, or render inline when the queue is unreachable."""
    r = _redis()
    if r is not None:
        try:
            def push():
                pipe = r.pipeline()
                pipe.zadd(QUEUE_KEY, {_member(player_id, fingerprint): time.time()}, nx=True)
                pipe.zremrangebyrank(QUEUE_KEY, 0, -(MAX_LEN + 1))
                pipe.execute()

            await asyncio.to_thread(push)
            return
        except Exception as exc:
            print(f"Gear render queue unavailable ({exc}); rendering inline")
    from services.gear_image import render_gear_image

    await render_gear_image(player_id, fingerprint)


async def _drain_once(r) -> bool:
    """Render the newest queued outfit. False when there was nothing to do."""
    popped = await asyncio.to_thread(r.zpopmax, QUEUE_KEY)
    if not popped:
        return False
    member, score = popped[0]
    if time.time() - float(score) > MAX_AGE_SECONDS:
        return True
    job = _parse(member)
    if job is None:
        return True

    from services.gear_image import render_gear_image
    from services.page_screenshot import ScreenshotBusy

    try:
        await render_gear_image(*job, raise_busy=True,
                                queue_timeout=_SLOT_WAIT_SECONDS)
    except ScreenshotBusy:
        # Back in line where it was; someone else's slot frees up soon.
        await asyncio.to_thread(r.zadd, QUEUE_KEY, {member: float(score)}, nx=True)
        await asyncio.sleep(_BUSY_BACKOFF_SECONDS)
    except Exception as exc:
        print(f"Queued gear render failed for player {job[0]}: {exc}")
    return True


async def _drainer() -> None:
    while True:
        r = _redis()
        try:
            if r is None or not await _drain_once(r):
                await asyncio.sleep(_IDLE_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Gear render drainer error: {exc}")
            await asyncio.sleep(_IDLE_POLL_SECONDS * 5)


def start(n: int = DRAINERS) -> list[asyncio.Task]:
    """Start the drainers on the running loop; cancel the tasks to stop."""
    return [asyncio.create_task(_drainer(), name=f"gear-render-drainer-{i}")
            for i in range(n)]
