"""Holding a scheduled event end while intake recovers (t274).

After downtime the events worker used to end every overdue event on its first
sweep, minutes before the R2 drain or the webhook catch-up replayed anything,
so the outage's submissions never counted. A queue backlog at an event's end
lost credit the same way. The sweep now waits while a recovery could still
deliver something received before the end, up to a cap.

Every signal fails open: an unreadable or missing signal must end the event on
schedule (the old behaviour), never hold it forever.
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from utils import event_end_hold as eh

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "_event_lifecycle_for_end_hold", os.path.join(_ROOT, "services", "event_lifecycle.py"))
lc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lc
_spec.loader.exec_module(lc)

ENDS = datetime(2026, 10, 5, 12, 0)
CAP = 2 * 3600


class FakeRedis:
    def __init__(self, kv=None, lists=None):
        self.kv = dict(kv or {})
        self.lists = {k: list(v) for k, v in (lists or {}).items()}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = str(value)
        return True

    def delete(self, key):
        self.kv.pop(key, None)

    def lindex(self, key, idx):
        items = self.lists.get(key) or []
        try:
            return items[idx]
        except IndexError:
            return None


class Broken:
    def __getattr__(self, name):
        def boom(*a, **k):
            raise ConnectionError("redis down")
        return boom


# ── decide() ─────────────────────────────────────────────────────────────────

class TestDecide:
    def test_no_signals_ends_on_schedule(self):
        d = eh.decide(ENDS, ENDS + timedelta(minutes=1), [], CAP)
        assert not d.hold and not d.capped

    def test_recovery_from_before_the_end_holds(self):
        sig = eh.Signal("r2_spool", ENDS - timedelta(hours=2))
        d = eh.decide(ENDS, ENDS + timedelta(minutes=1), [sig], CAP)
        assert d.hold and d.reasons == ["r2_spool"]

    def test_recovery_that_started_after_the_end_is_irrelevant(self):
        sig = eh.Signal("r2_spool", ENDS + timedelta(minutes=5))
        assert not eh.decide(ENDS, ENDS + timedelta(minutes=10), [sig], CAP).hold

    def test_unknown_start_counts_as_relevant(self):
        d = eh.decide(ENDS, ENDS + timedelta(minutes=1), [eh.Signal("disk_spool")], CAP)
        assert d.hold

    def test_cap_ends_it_and_flags_it(self):
        sig = eh.Signal("boot_gap", ENDS - timedelta(hours=1))
        d = eh.decide(ENDS, ENDS + timedelta(seconds=CAP), [sig], CAP)
        assert not d.hold and d.capped and d.reasons == ["boot_gap"]

    def test_kill_switch_and_unscheduled_events_never_hold(self):
        sig = eh.Signal("boot_gap", ENDS - timedelta(hours=1))
        assert not eh.decide(ENDS, ENDS, [sig], 0).hold
        assert not eh.decide(None, ENDS, [sig], CAP).hold

    def test_env_cap(self, monkeypatch):
        monkeypatch.delenv("EVENT_END_HOLD_MAX_SECONDS", raising=False)
        assert eh.hold_max_seconds() == 7200
        monkeypatch.setenv("EVENT_END_HOLD_MAX_SECONDS", "0")
        assert eh.hold_max_seconds() == 0
        monkeypatch.setenv("EVENT_END_HOLD_MAX_SECONDS", "junk")
        assert eh.hold_max_seconds() == 7200


# ── gather_signals() ─────────────────────────────────────────────────────────

class TestGatherSignals:
    def test_quiet_box_raises_nothing(self):
        assert eh.gather_signals(FakeRedis(), spool_count=lambda: 0) == []

    def test_each_marker_is_read_with_its_start(self):
        boot = ENDS - timedelta(hours=3)
        r = FakeRedis(kv={
            eh.BOOT_RECOVERY_KEY: str(int(boot.timestamp())),
            eh.R2_RECOVERY_KEY: "2026-10-05T10:00:00.000Z",
            # snowflake for 2026-10-05 09:00 UTC
            eh.CATCHUP_ACTIVE_KEY: str((int(datetime(2026, 10, 5, 9, 0).timestamp() * 1000)
                                        - 1420070400000) << 22),
        })
        got = {s.reason: s.since for s in eh.gather_signals(r)}
        assert got["boot_gap"] == boot
        assert got["r2_spool"] == datetime(2026, 10, 5, 10, 0)
        assert got["webhook_catchup"] == datetime(2026, 10, 5, 9, 0)

    def test_backlogs_report_their_oldest_entry(self):
        old = datetime(2026, 10, 5, 11, 0)
        r = FakeRedis(lists={
            eh.INTAKE_QUEUE_KEY: [json.dumps({"enqueued_at": "2026-10-05T11:50:00"}),
                                  json.dumps({"enqueued_at": old.isoformat()})],
            eh.EVENT_QUEUE_KEY: [json.dumps({"ts": int(old.timestamp())})],
        })
        got = {s.reason: s.since for s in eh.gather_signals(r)}
        assert got == {"intake_backlog": old, "event_backlog": old}

    def test_unreadable_backlog_entry_counts_as_unknown_start(self):
        r = FakeRedis(lists={eh.INTAKE_QUEUE_KEY: ["not json"]})
        assert eh.gather_signals(r) == [eh.Signal("intake_backlog", None)]

    def test_disk_spool(self):
        assert eh.gather_signals(FakeRedis(), spool_count=lambda: 3) == [
            eh.Signal("disk_spool", None)]

    def test_redis_errors_fail_open(self):
        assert eh.gather_signals(Broken(), spool_count=lambda: 0) == []
        assert eh.gather_signals(None) == []

    def test_spool_count_error_fails_open(self):
        def boom():
            raise OSError("no dir")
        assert eh.gather_signals(FakeRedis(), spool_count=boom) == []


# ── boot detection ───────────────────────────────────────────────────────────

class TestBoot:
    def test_stale_heartbeat_raises_the_boot_marker(self):
        r = FakeRedis(kv={eh.HEARTBEAT_KEY: "1000"})
        assert eh.note_boot(r, 1000 + 3600) == 3600
        assert r.kv[eh.BOOT_RECOVERY_KEY] == "1000"
        assert eh.boot_gap_since(r) == datetime.fromtimestamp(1000)

    def test_a_deploy_restart_is_not_an_outage(self):
        r = FakeRedis(kv={eh.HEARTBEAT_KEY: "1000"})
        assert eh.note_boot(r, 1000 + 90) is None
        assert eh.BOOT_RECOVERY_KEY not in r.kv

    def test_no_heartbeat_yet_raises_nothing(self):
        r = FakeRedis()
        assert eh.note_boot(r, 5000) is None
        assert eh.BOOT_RECOVERY_KEY not in r.kv

    def test_heartbeat_write_and_errors(self):
        r = FakeRedis()
        eh.write_heartbeat(r, 1234.5)
        assert r.kv[eh.HEARTBEAT_KEY] == "1234"
        eh.write_heartbeat(Broken(), 1)        # never raises
        assert eh.note_boot(Broken(), 1) is None
        assert eh.boot_gap_since(Broken()) is None


# ── the sweep ────────────────────────────────────────────────────────────────

class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    def query(self, *a):
        return _Query(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _event(id=1, ends_at=ENDS, **kw):
    base = dict(id=id, name=f"Event {id}", status="active", starts_at=ENDS - timedelta(days=1),
                ends_at=ends_at, ended_at=None, kind="standard", mode="standard")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def sweep(monkeypatch):
    """run_lifecycle_sweep with end_event, notices and the side loops stubbed."""
    ended, notices = [], []
    monkeypatch.setattr(lc, "end_event", lambda s, e, now=None, **k: ended.append(e.id))
    monkeypatch.setattr(lc, "_enqueue_recovery_notice",
                        lambda s, e, variant, title, body: notices.append((e.id, variant, body)))
    monkeypatch.setattr(lc, "run_window_sweep", lambda *a, **k: None)
    monkeypatch.setattr(lc, "run_reminder_sweep", lambda *a, **k: None)
    monkeypatch.delenv("EVENT_END_HOLD_MAX_SECONDS", raising=False)

    def run(rows, redis_conn, now, signals):
        monkeypatch.setattr(lc, "_gather_hold_signals", lambda r: list(signals))
        summary = lc.run_lifecycle_sweep(_Session(rows), redis_conn, now=now)
        return summary
    return SimpleNamespace(run=run, ended=ended, notices=notices)


class TestSweepHold:
    def test_quiet_box_ends_on_schedule(self, sweep):
        s = sweep.run([_event()], FakeRedis(), ENDS + timedelta(seconds=30), [])
        assert sweep.ended == [1] and s["held"] == [] and sweep.notices == []

    def test_recovery_holds_and_notifies_once(self, sweep):
        r = FakeRedis()
        sig = [eh.Signal("boot_gap", ENDS - timedelta(hours=1))]
        for minute in (1, 2, 3):
            s = sweep.run([_event()], r, ENDS + timedelta(minutes=minute), sig)
            assert s["held"] == [1]
        assert sweep.ended == []
        assert [n[1] for n in sweep.notices] == ["held"]

    def test_release_after_recovery_clears_the_marker(self, sweep):
        r = FakeRedis()
        sweep.run([_event()], r, ENDS + timedelta(minutes=1),
                  [eh.Signal("r2_spool", ENDS - timedelta(minutes=30))])
        assert eh.HOLD_MARKER_KEY.format(event_id=1) in r.kv
        sweep.run([_event()], r, ENDS + timedelta(minutes=20), [])
        assert sweep.ended == [1]
        assert eh.HOLD_MARKER_KEY.format(event_id=1) not in r.kv
        assert [n[1] for n in sweep.notices] == ["held"]

    def test_cap_ends_with_a_capped_notice(self, sweep):
        sig = [eh.Signal("r2_spool", ENDS - timedelta(hours=1))]
        sweep.run([_event()], FakeRedis(), ENDS + timedelta(hours=2, minutes=1), sig)
        assert sweep.ended == [1]
        assert [n[1] for n in sweep.notices] == ["capped"]

    def test_only_events_inside_the_gap_wait(self, sweep):
        early = _event(1, ends_at=ENDS - timedelta(hours=2))
        late = _event(2, ends_at=ENDS)
        sig = [eh.Signal("r2_spool", ENDS - timedelta(hours=1))]
        s = sweep.run([early, late], FakeRedis(), ENDS + timedelta(minutes=1), sig)
        assert sweep.ended == [1] and s["held"] == [2]

    def test_kill_switch(self, sweep, monkeypatch):
        sig = [eh.Signal("boot_gap", ENDS - timedelta(hours=1))]

        def run_off():
            monkeypatch.setattr(lc, "_gather_hold_signals", lambda r: sig)
            monkeypatch.setenv("EVENT_END_HOLD_MAX_SECONDS", "0")
            return lc.run_lifecycle_sweep(_Session([_event()]), FakeRedis(),
                                          now=ENDS + timedelta(minutes=1))
        assert run_off()["held"] == []
        assert sweep.ended == [1]

    def test_no_redis_never_holds(self, sweep):
        sig = [eh.Signal("boot_gap", ENDS - timedelta(hours=1))]
        sweep.run([_event()], None, ENDS + timedelta(minutes=1), sig)
        assert sweep.ended == [1]

    def test_held_event_skips_the_per_tick_loops(self, sweep, monkeypatch):
        settled = []
        conquest = _event(kind="conquest")
        fake = SimpleNamespace(settle_conquest=lambda *a, **k: settled.append(1))
        monkeypatch.setitem(sys.modules, "services.conquest_engine", fake)
        sweep.run([conquest], FakeRedis(), ENDS + timedelta(minutes=1),
                  [eh.Signal("boot_gap", ENDS - timedelta(hours=1))])
        assert settled == []


class TestScoringEnd:
    def test_held_end_settles_at_the_scheduled_end(self):
        ev = _event(ended_at=ENDS + timedelta(hours=1))
        assert lc._scoring_end(ev, ENDS + timedelta(hours=1)) == ENDS

    def test_premature_manual_end_uses_the_real_end(self):
        ev = _event(ended_at=ENDS - timedelta(hours=3))
        assert lc._scoring_end(ev, ENDS) == ENDS - timedelta(hours=3)

    def test_unscheduled(self):
        now = datetime(2026, 1, 1)
        assert lc._scoring_end(_event(ends_at=None, ended_at=None), now) == now


class TestBootGapLateStart:
    """t274 phase 4: a draft whose start fell inside an events-worker outage
    is activated with its scheduled start, so its window opens on time."""

    @pytest.fixture()
    def activations(self, monkeypatch):
        calls = []
        monkeypatch.setattr(lc, "activate_event",
                            lambda s, e, now=None, activated_at=None, **k:
                            calls.append((e.id, activated_at)))
        monkeypatch.setattr(lc, "_clear_activation_failure", lambda *a: None)
        monkeypatch.setattr(lc, "run_window_sweep", lambda *a, **k: None)
        monkeypatch.setattr(lc, "run_reminder_sweep", lambda *a, **k: None)
        monkeypatch.setattr(lc, "_gather_hold_signals", lambda r: [])
        return calls

    def _draft(self, eid, starts_at):
        return _event(eid, status="draft", starts_at=starts_at, ends_at=starts_at + timedelta(days=2))

    def test_start_inside_the_gap_keeps_its_schedule(self, activations):
        down_from = datetime(2026, 10, 5, 9, 0)
        r = FakeRedis(kv={eh.BOOT_RECOVERY_KEY: str(int(down_from.timestamp()))})
        starts = datetime(2026, 10, 5, 10, 0)
        lc.run_lifecycle_sweep(_Session([self._draft(3, starts)]), r,
                               now=datetime(2026, 10, 5, 13, 0))
        assert activations == [(3, starts)]

    def test_start_before_the_gap_or_no_gap_is_stamped_now(self, activations):
        down_from = datetime(2026, 10, 5, 9, 0)
        r = FakeRedis(kv={eh.BOOT_RECOVERY_KEY: str(int(down_from.timestamp()))})
        now = datetime(2026, 10, 5, 13, 0)
        lc.run_lifecycle_sweep(_Session([self._draft(4, datetime(2026, 10, 5, 8, 0))]), r, now=now)
        lc.run_lifecycle_sweep(_Session([self._draft(5, datetime(2026, 10, 5, 10, 0))]),
                               FakeRedis(), now=now)
        assert activations == [(4, None), (5, None)]
