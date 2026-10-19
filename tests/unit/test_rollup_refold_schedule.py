"""Which days the periodic rollup re-fold recomputes (services/rollup_refold.py).

The hourly pass used to recompute the whole current month. It now recomputes
the recent days plus any day flagged dirty, and the whole month only every
FULL_INTERVAL_SEC. What must hold for that to stay correct:

* an hourly pass covers today and the recent window, and every dirty day the
  full plan covers -- never a day the full plan wouldn't;
* a dirty mark survives until its day has been recomputed successfully, so a
  failed day (or an edit landing mid-pass) is retried rather than lost;
* the Discord edit/delete marks are fail-open: a Redis fault never raises.
"""
import asyncio
import importlib.util
import os
import sys
from datetime import date, datetime, timedelta

import pytest

_SERVICES = os.path.join(os.path.dirname(__file__), "..", "..", "services")


def _load_real():
    path = os.path.abspath(os.path.join(_SERVICES, "rollup_refold.py"))
    spec = importlib.util.spec_from_file_location("_real_rollup_refold", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRedis:
    def __init__(self, fail=False):
        self.sets = {}
        self.fail = fail
        self.expires = {}

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def sadd(self, key, member):
        self._check()
        self.sets.setdefault(key, set()).add(member.encode())

    def srem(self, key, member):
        self._check()
        self.sets.get(key, set()).discard(member.encode())

    def smembers(self, key):
        self._check()
        return set(self.sets.get(key, set()))

    def expire(self, key, ttl):
        self._check()
        self.expires[key] = ttl


@pytest.fixture
def rr(monkeypatch):
    module = _load_real()
    fake = _FakeRedis()
    monkeypatch.setattr(module, "_redis", lambda: fake)
    module._fake = fake
    return module


def _plan(days):
    return [(int(d.strftime("%Y%m")), d, d + timedelta(days=1)) for d in days]


# --------------------------------------------------------------------------- #
# select_plan                                                                  #
# --------------------------------------------------------------------------- #
def test_recent_window_covers_today_and_yesterday_only(rr):
    today = date(2026, 10, 19)
    month = [date(2026, 10, d) for d in range(1, 32)]
    picked = rr.select_plan(_plan(month), today, dirty=set(), recent_days=2)
    assert [e[1] for e in picked] == [date(2026, 10, 18), date(2026, 10, 19)]


def test_future_days_of_the_month_are_skipped(rr):
    today = date(2026, 10, 19)
    picked = rr.select_plan(_plan([date(2026, 10, 20), date(2026, 10, 31)]), today, set(), 2)
    assert picked == []


def test_dirty_days_inside_the_plan_are_added(rr):
    today = date(2026, 10, 19)
    month = [date(2026, 10, d) for d in range(1, 32)]
    picked = rr.select_plan(_plan(month), today, {date(2026, 10, 3)}, 2)
    assert [e[1] for e in picked] == [date(2026, 10, 3), date(2026, 10, 18), date(2026, 10, 19)]


def test_dirty_days_the_full_plan_does_not_cover_stay_uncovered(rr):
    """Same coverage as before: an edit on a closed month was never re-folded."""
    today = date(2026, 10, 19)
    month = [date(2026, 10, d) for d in range(1, 32)]
    picked = rr.select_plan(_plan(month), today, {date(2026, 7, 4)}, 2)
    assert date(2026, 7, 4) not in [e[1] for e in picked]


def test_window_crosses_the_month_boundary_with_its_own_partition(rr):
    today = date(2026, 11, 1)
    plan = _plan([date(2026, 10, d) for d in range(1, 32)] + [date(2026, 11, d) for d in range(1, 31)])
    picked = rr.select_plan(plan, today, set(), 2)
    assert [(e[0], e[1]) for e in picked] == [(202610, date(2026, 10, 31)), (202611, date(2026, 11, 1))]


# --------------------------------------------------------------------------- #
# marks                                                                        #
# --------------------------------------------------------------------------- #
def test_mark_dirty_flags_both_rollups_with_a_ttl(rr):
    rr.mark_dirty(datetime(2026, 10, 3, 14, 5))
    for kind in rr.KINDS:
        assert rr.dirty_days(kind) == {date(2026, 10, 3)}
        assert rr._fake.expires[rr.DIRTY_KEY.format(kind=kind)] == rr.DIRTY_TTL_SEC


def test_unmark_clears_one_rollup_only(rr):
    rr.mark_dirty(date(2026, 10, 3))
    rr.unmark("item_totals", date(2026, 10, 3))
    assert rr.dirty_days("item_totals") == set()
    assert rr.dirty_days("npc_totals") == {date(2026, 10, 3)}


def test_garbage_members_are_ignored(rr):
    rr._fake.sets[rr.DIRTY_KEY.format(kind="item_totals")] = {b"2026-10-03", b"not-a-date"}
    assert rr.dirty_days("item_totals") == {date(2026, 10, 3)}


def test_missing_date_is_ignored(rr):
    rr.mark_dirty(None)
    assert rr._fake.sets == {}


def test_redis_faults_never_raise(rr, monkeypatch):
    broken = _FakeRedis(fail=True)
    monkeypatch.setattr(rr, "_redis", lambda: broken)
    rr.mark_dirty(date(2026, 10, 3))
    rr.unmark("item_totals", date(2026, 10, 3))
    rr.remark("item_totals", date(2026, 10, 3))
    assert rr.dirty_days("item_totals") == set()


# --------------------------------------------------------------------------- #
# _run_periodic_refold                                                         #
# --------------------------------------------------------------------------- #
class _FakeRollup:
    """Stands in for services.item_totals: records which days were re-folded."""

    REFOLD_PACE_SEC = 0

    def __init__(self, days, fail_on=()):
        self.days = days
        self.fail_on = set(fail_on)
        self.refolded = []

    def get_pointer(self):
        return 1000

    def refold_plan(self):
        return _plan(self.days)

    def refold_day(self, session, partition, day_start, day_end, cap):
        if day_start in self.fail_on:
            raise RuntimeError("boom")
        self.refolded.append(day_start)
        return 1


class _NullSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def rollback(self):
        pass


@pytest.fixture
def ptu(monkeypatch, rr):
    import data.player_total_updater as module

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "Session", _NullSession)
    monkeypatch.setattr(module, "send_watchdog_heartbeat", _noop)
    monkeypatch.setattr(module, "sleep_with_watchdog_heartbeats", _noop)
    monkeypatch.setattr(sys.modules["services"], "rollup_refold", rr, raising=False)
    return module


def _month_to_today():
    today = datetime.now().date()
    return [today - timedelta(days=n) for n in range(10, -1, -1)]


def test_recent_pass_refolds_recent_and_dirty_days_and_clears_marks(ptu, rr):
    days = _month_to_today()
    today = days[-1]
    old_dirty = days[0]
    rr.mark_dirty(old_dirty)
    rollup = _FakeRollup(days)

    asyncio.run(ptu._run_periodic_refold(rollup, "item_totals", full=False))

    assert rollup.refolded == [old_dirty] + [d for d in days if (today - d).days < rr.RECENT_DAYS]
    assert rr.dirty_days("item_totals") == set()
    # The other rollup's mark is untouched until its own pass.
    assert rr.dirty_days("npc_totals") == {old_dirty}


def test_failed_dirty_day_keeps_its_mark(ptu, rr):
    days = _month_to_today()
    old_dirty = days[0]
    rr.mark_dirty(old_dirty)
    rollup = _FakeRollup(days, fail_on={old_dirty})

    asyncio.run(ptu._run_periodic_refold(rollup, "item_totals", full=False))

    assert rr.dirty_days("item_totals") == {old_dirty}


def test_full_pass_refolds_every_day_and_clears_marks(ptu, rr):
    days = _month_to_today()
    rr.mark_dirty(days[3])
    rollup = _FakeRollup(days)

    asyncio.run(ptu._run_periodic_refold(rollup, "item_totals", full=True))

    assert rollup.refolded == days
    assert rr.dirty_days("item_totals") == set()


def test_unseeded_tailer_skips_the_pass(ptu, rr):
    rollup = _FakeRollup(_month_to_today())
    rollup.get_pointer = lambda: None
    rr.mark_dirty(rollup.days[0])

    asyncio.run(ptu._run_periodic_refold(rollup, "item_totals", full=False))

    assert rollup.refolded == []
    assert rr.dirty_days("item_totals") == {rollup.days[0]}


# --------------------------------------------------------------------------- #
# the Discord paths that change an existing drop flag its day                  #
# --------------------------------------------------------------------------- #
def _calls_in(func_name):
    import ast

    path = os.path.abspath(os.path.join(_SERVICES, "entry_modifier.py"))
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            return {
                f"{c.func.value.id}.{c.func.attr}"
                for c in ast.walk(node)
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and isinstance(c.func.value, ast.Name)
            }
    raise AssertionError(f"{func_name} not found in services/entry_modifier.py")


@pytest.mark.parametrize("handler", ["_handle_delete_confirm", "_process_edit_value"])
def test_modify_entry_handlers_mark_the_drop_day_dirty(handler):
    """Without the mark, an edit to a drop older than RECENT_DAYS would only
    reach item/NPC totals on the next full pass instead of within the hour."""
    assert "rollup_refold.mark_dirty" in _calls_in(handler)


class _Abort(BaseException):
    """Not an Exception: escapes the per-day handler like a cancellation."""


def test_an_aborted_pass_keeps_the_mark_of_the_day_it_was_on(ptu, rr):
    days = _month_to_today()
    old_dirty = days[0]
    rr.mark_dirty(old_dirty)
    rollup = _FakeRollup(days)

    def _abort(*args, **kwargs):
        raise _Abort()

    rollup.refold_day = _abort
    with pytest.raises(_Abort):
        asyncio.run(ptu._run_periodic_refold(rollup, "item_totals", full=False))

    assert rr.dirty_days("item_totals") == {old_dirty}
