"""Concurrent consumer worker behavior.

The consumer runs NUM_WORKERS parallel brpoplpush -> _process_entry loops.
A popped entry is the only remaining copy of a submission the API already
ACCEPTED, so the worker loop must (a) hold it in the PROCESSING list until it
is resolved, so a hard kill leaves it recoverable, (b) dead-letter entries
whose processing raises instead of dropping them, (c) remove it from
PROCESSING either way, or the next restart's reclaim would apply it twice,
and (d) finish the in-flight entry before honoring a stop request.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import workers.webhook_consumer as wc


def _redis_returning(entries, stop):
    """Fake redis: brpoplpush yields each entry once, then sets stop and idles."""
    r = MagicMock()
    remaining = list(entries)

    def brpoplpush(src, dst, timeout):
        if remaining:
            return remaining.pop(0)
        stop.set()
        return None

    r.brpoplpush.side_effect = brpoplpush
    return r


async def test_worker_dead_letters_failed_entry(monkeypatch):
    stop = asyncio.Event()
    entry = json.dumps({"payload": {}}).encode()
    r = _redis_returning([entry], stop)

    boom = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(wc, "_process_entry", boom)

    await wc._worker(0, r, stop)

    boom.assert_awaited_once_with(entry)
    pipe = r.pipeline.return_value
    pipe.lpush.assert_called_once_with(wc.DEAD_KEY, entry)
    pipe.ltrim.assert_called_once_with(wc.DEAD_KEY, 0, wc.DEAD_MAX - 1)
    pipe.execute.assert_called_once()
    assert wc._in_flight == 0  # decremented even on failure
    # Dead-lettered is resolved: leaving it in PROCESSING would re-apply it
    # on the next restart's reclaim, on top of the dead-letter copy.
    r.lrem.assert_any_call(wc.PROCESSING_KEY, 1, entry)


async def test_worker_processes_without_dead_letter(monkeypatch):
    stop = asyncio.Event()
    entry = json.dumps({"payload": {}}).encode()
    r = _redis_returning([entry], stop)

    ok = AsyncMock()
    monkeypatch.setattr(wc, "_process_entry", ok)

    await wc._worker(0, r, stop)

    ok.assert_awaited_once_with(entry)
    r.pipeline.assert_not_called()
    assert wc._in_flight == 0
    r.lrem.assert_called_once_with(wc.PROCESSING_KEY, 1, entry)


async def test_worker_finishes_in_flight_entry_on_stop(monkeypatch):
    """A stop request mid-entry must not abandon the popped entry."""
    stop = asyncio.Event()
    entry = json.dumps({"payload": {}}).encode()
    r = _redis_returning([entry], stop)

    finished = []

    async def slow_process(entry_bytes):
        stop.set()  # stop requested while this entry is in flight
        await asyncio.sleep(0)
        finished.append(entry_bytes)

    monkeypatch.setattr(wc, "_process_entry", slow_process)

    await wc._worker(0, r, stop)

    assert finished == [entry]


async def test_worker_exits_without_popping_when_already_stopped():
    stop = asyncio.Event()
    stop.set()
    r = MagicMock()

    await wc._worker(0, r, stop)

    r.brpoplpush.assert_not_called()


def test_reclaim_returns_stranded_entries_to_the_queue():
    """Entries a killed worker was mid-processing must go back on the queue.

    This is the whole point of the PROCESSING list: without the reclaim it
    just relocates the loss instead of preventing it.
    """
    r = MagicMock()
    stranded = [b"a", b"b"]
    r.lmove.side_effect = lambda src, dst, f, t: stranded.pop(0) if stranded else None

    moved = wc._reclaim_inflight(r)

    assert moved == 2
    # RIGHT->RIGHT: recovered entries are the oldest accepted work in the
    # system and land at the consumer's pop end, ahead of newer traffic.
    # (The queue is FIFO — acceptor LPUSHes, workers pop the right end; a
    # left-landing reclaim would park them behind the entire backlog.)
    r.lmove.assert_called_with(wc.PROCESSING_KEY, wc.QUEUE_KEY, "RIGHT", "RIGHT")


def test_reclaim_is_quiet_when_nothing_was_stranded():
    r = MagicMock()
    r.lmove.return_value = None

    assert wc._reclaim_inflight(r) == 0
