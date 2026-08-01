"""Concurrent consumer worker behavior.

The consumer runs NUM_WORKERS parallel blpop -> _process_entry loops. A
BLPOP'd entry is the only remaining copy of a submission, so the worker loop
must (a) dead-letter entries whose processing raises instead of dropping
them, and (b) finish the in-flight entry before honoring a stop request.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import workers.webhook_consumer as wc


def _redis_returning(entries, stop):
    """Fake redis: blpop yields each entry once, then sets stop and idles."""
    r = MagicMock()
    remaining = list(entries)

    def blpop(key, timeout):
        if remaining:
            return (wc.QUEUE_KEY.encode(), remaining.pop(0))
        stop.set()
        return None

    r.blpop.side_effect = blpop
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

    r.blpop.assert_not_called()
