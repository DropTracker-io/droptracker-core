"""Re-fold hour chunking for the item/npc hourly rollups.

A whole day per statement upserted ~100k rows and took 25-35s against the
engine's 30s ``read_timeout``, so days tipped over the limit at random and
never healed (2026-07-29: 12 of 28 days failed on every pass, silently leaving
the tailer's commit-order gaps unrepaired). ``refold_day`` now issues one
statement per ``REFOLD_CHUNK_HOURS``.

What must hold for that to stay correct: the chunks have to tile the day
*exactly* — no gap (a missed hour is a permanently unhealed bucket) and no
overlap past the day boundary (which would let a chunk recompute a bucket using
only part of its rows). Both modules are near-identical clones, so both are
checked.
"""
import importlib.util
import os
import sys
from datetime import date, datetime, timedelta

import pytest

_SERVICES = os.path.join(os.path.dirname(__file__), "..", "..", "services")
MODULE_IDS = ["item_totals", "npc_totals"]


def _load(name):
    """Load the real services/<name>.py despite the conftest stub."""
    path = os.path.abspath(os.path.join(_SERVICES, f"{name}.py"))
    spec = importlib.util.spec_from_file_location(f"_real_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"_real_{name}"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(f"_real_{name}", None)
        raise
    return module


@pytest.fixture(params=MODULE_IDS, ids=MODULE_IDS)
def module(request):
    name = request.param
    mod = _load(name)
    try:
        yield mod
    finally:
        sys.modules.pop(f"_real_{name}", None)



def test_chunks_tile_the_day_exactly(module):
    day = date(2026, 7, 22)
    chunks = list(module._hour_chunks(day, day + timedelta(days=1)))

    assert chunks, "a day must produce at least one chunk"
    # Contiguous: each chunk starts exactly where the previous ended, so no
    # hour of the day is skipped and none is covered twice.
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert prev_end == next_start

    assert chunks[0][0] == datetime(2026, 7, 22, 0, 0)
    assert chunks[-1][1] == datetime(2026, 7, 23, 0, 0)


def test_chunks_are_hour_aligned(module):
    """`date_hour` buckets are whole hours, so a chunk boundary landing
    mid-hour would split one bucket across two statements — each recomputing
    it from half its rows, the second overwriting the first."""
    day = date(2026, 7, 22)
    for start, end in module._hour_chunks(day, day + timedelta(days=1)):
        assert (start.minute, start.second, start.microsecond) == (0, 0, 0)
        assert (end.minute, end.second, end.microsecond) == (0, 0, 0)


def test_chunk_size_never_scans_past_the_day(module, monkeypatch):
    """A chunk size that doesn't divide 24 must clamp its last chunk to the day
    boundary rather than spilling into the next day, which would double-count
    the next day's first buckets against a partial row set."""
    monkeypatch.setattr(module, "REFOLD_CHUNK_HOURS", 5)
    day = date(2026, 7, 22)
    chunks = list(module._hour_chunks(day, day + timedelta(days=1)))
    assert chunks[-1][1] == datetime(2026, 7, 23, 0, 0)
    assert all(s < e for s, e in chunks), "no empty or inverted chunk"


def test_accepts_datetimes_as_well_as_dates(module):
    """refold_plan yields dates today, but the driver passes them straight
    through; accepting datetimes keeps a caller passing a narrower window from
    silently producing nothing."""
    start = datetime(2026, 7, 22, 6, 0)
    chunks = list(module._hour_chunks(start, datetime(2026, 7, 22, 9, 0)))
    assert chunks[0][0] == start
    assert chunks[-1][1] == datetime(2026, 7, 22, 9, 0)


def test_refold_day_commits_each_chunk_and_sums_rowcounts(module):
    """One statement + commit per chunk (so a slow day can't hold a single
    transaction open across the whole day), and the returned rowcount is the
    total across chunks so the driver's "touched N rows" stays truthful."""

    class FakeResult:
        rowcount = 3

    class FakeSession:
        def __init__(self):
            self.windows = []
            self.commits = 0

        def execute(self, _sql, params):
            self.windows.append((params["day_start"], params["day_end"]))
            return FakeResult()

        def commit(self):
            self.commits += 1

    session = FakeSession()
    day = date(2026, 7, 22)
    affected = module.refold_day(session, 202607, day, day + timedelta(days=1), 999)

    assert len(session.windows) == 24
    assert session.commits == 24
    assert affected == 72
    # Every statement is bounded by its own chunk, never the whole day.
    assert session.windows[0] == (datetime(2026, 7, 22, 0, 0), datetime(2026, 7, 22, 1, 0))
    assert session.windows[-1] == (datetime(2026, 7, 22, 23, 0), datetime(2026, 7, 23, 0, 0))
