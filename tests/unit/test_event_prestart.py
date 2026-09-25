"""Pre-start parking + late-activation window (2026-09-25, event 81).

A scheduled draft goes live on the lifecycle sweep, up to a minute after its
starts_at. Submissions in that gap used to be discarded: the draft wasn't in
the matcher state, and once live its window opened at the activation stamp.
These cover the three pieces of the fix: the window rule
(utils.event_window), parking a pinned copy for due drafts, and replaying it.

The engine is loaded from its file path like test_event_engine_matcher, so the
conftest stubs for db/redis/services never interfere.
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

from utils.event_window import LATE_START_GRACE_SECONDS, effective_window_start

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_prestart_ut", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_prestart_ut"] = engine
_spec.loader.exec_module(engine)

START = datetime(2026, 9, 25, 14, 0, 0)


def _ts(dt):
    return int(dt.timestamp())


def _env(player_id=5, at=START + timedelta(seconds=12), **kw):
    env = {"v": 1, "kind": "drop", "guid": "g-1", "player_id": player_id,
           "ts": _ts(at), "used_api": True,
           "data": {"item_name": "Verac's plateskirt", "quantity": 1}}
    env.update(kw)
    return env


class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        def call(*a):
            self.ops.append((name, a))
            return self
        return call

    def execute(self):
        return [getattr(self.r, name)(*a) for name, a in self.ops]


class _FakeRedis:
    def __init__(self):
        self.lists, self.ttl = {}, {}

    def pipeline(self):
        return _Pipe(self)

    def rpush(self, key, *vals):
        self.lists.setdefault(key, []).extend(
            v.encode() if isinstance(v, str) else v for v in vals)
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        lst = self.lists.get(key, [])
        end = len(lst) if end == -1 else end + 1
        self.lists[key] = lst[start:end] if start >= 0 else lst[max(len(lst) + start, 0):end]
        return True

    def expire(self, key, secs):
        self.ttl[key] = secs
        return True

    def exists(self, key):
        return int(bool(self.lists.get(key)))

    def rename(self, src, dst):
        if src not in self.lists:
            raise RuntimeError("no such key")
        self.lists[dst] = self.lists.pop(src)

    def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    def delete(self, key):
        self.lists.pop(key, None)


def _state(**kw):
    st = engine.MatcherState(
        prestart={81: START},
        prestart_participants={5: [(81, START - timedelta(hours=14))]},
    )
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _parked(r, event_id=81):
    return [json.loads(b) for b in r.lists.get(f"events:prestart:{event_id}", [])]


# ── window rule ───────────────────────────────────────────────────────────────

class TestEffectiveWindowStart:
    def test_sweep_lag_keeps_the_scheduled_start(self):
        # Event 81: scheduled 14:00:00, the sweep activated it at 14:00:38.
        assert effective_window_start(START, START + timedelta(seconds=38)) == START

    def test_activation_at_the_grace_edge_keeps_the_scheduled_start(self):
        at = START + timedelta(seconds=LATE_START_GRACE_SECONDS)
        assert effective_window_start(START, at) == START

    def test_much_later_activation_runs_from_activation(self):
        at = START + timedelta(seconds=LATE_START_GRACE_SECONDS + 1)
        assert effective_window_start(START, at) == at

    def test_early_start_now_keeps_the_scheduled_start(self):
        # Unchanged behaviour: max(starts_at, activated_at).
        assert effective_window_start(START, START - timedelta(days=1)) == START

    def test_missing_sides(self):
        assert effective_window_start(None, START) == START
        assert effective_window_start(START, None) == START
        assert effective_window_start(None, None) is None

    def test_engine_event_dict_uses_it(self):
        class _Ev:
            id, name, group_id = 81, "Luck of the Roll", 227
            requires_confirmation, has_bingo = False, False
            submission_policy, message_config, kind = "confirm_non_api", None, "board_game"
            board_size, bonus_line_points, bonus_blackout_points = 5, 0, 0
            starts_at, activated_at = START, START + timedelta(seconds=38)
            ends_at, ended_at, schedule_config = START + timedelta(days=5), None, None
        assert engine._event_to_dict(_Ev())["window_start"] == START


# ── parking ───────────────────────────────────────────────────────────────────

class TestParkPrestart:
    def test_member_submission_in_the_gap_is_parked_pinned(self):
        r = _FakeRedis()
        assert engine.park_prestart(r, _state(), _env()) == [81]
        (copy,) = _parked(r)
        assert copy["only_event_id"] == 81
        assert copy["data"]["item_name"] == "Verac's plateskirt"
        assert r.ttl["events:prestart:81"] == engine.PRESTART_TTL_SECONDS

    def test_before_the_start_is_not_parked(self):
        r = _FakeRedis()
        assert engine.park_prestart(r, _state(), _env(at=START - timedelta(seconds=1))) == []
        assert _parked(r) == []

    def test_past_the_grace_is_not_parked(self):
        r = _FakeRedis()
        late = START + timedelta(seconds=LATE_START_GRACE_SECONDS + 1)
        assert engine.park_prestart(r, _state(), _env(at=late)) == []

    def test_before_the_members_own_join_is_not_parked(self):
        r = _FakeRedis()
        st = _state(prestart_participants={5: [(81, START + timedelta(minutes=1))]})
        assert engine.park_prestart(r, st, _env()) == []

    def test_non_member_is_not_parked(self):
        r = _FakeRedis()
        assert engine.park_prestart(r, _state(), _env(player_id=6)) == []

    def test_wom_envelopes_are_not_parked(self):
        r = _FakeRedis()
        env = _env(data={"source": "wom", "skill": "attack", "xp": 1})
        assert engine.park_prestart(r, _state(), env) == []

    def test_a_replayed_copy_is_never_parked_again(self):
        r = _FakeRedis()
        assert engine.park_prestart(r, _state(), _env(only_event_id=81)) == []

    def test_retry_counter_is_not_carried_into_the_copy(self):
        r = _FakeRedis()
        engine.park_prestart(r, _state(), _env(_attempts=2))
        assert "_attempts" not in _parked(r)[0]

    def test_no_due_drafts_is_a_no_op(self):
        r = _FakeRedis()
        assert engine.park_prestart(r, engine.MatcherState(), _env()) == []

    def test_parks_once_per_event_for_each_due_draft(self):
        r = _FakeRedis()
        st = _state(prestart={81: START, 82: START},
                    prestart_participants={5: [(81, None), (82, None), (81, None)]})
        assert engine.park_prestart(r, st, _env()) == [81, 82]
        assert len(_parked(r, 81)) == 1 and len(_parked(r, 82)) == 1


# ── replay ────────────────────────────────────────────────────────────────────

class TestReplayPrestart:
    def test_requeues_oldest_first_at_the_consuming_end(self):
        r = _FakeRedis()
        r.rpush(engine.QUEUE_KEY, b"live-newer")
        for n in range(3):
            engine.park_prestart(r, _state(), _env(guid=f"g{n}",
                                                   at=START + timedelta(seconds=n)))
        assert engine.replay_prestart(r, [81, 99]) == {81: 3}
        queue = r.lists[engine.QUEUE_KEY]
        # The consumer RPOPs: the replayed copies come off first, oldest first.
        popped = [json.loads(b)["guid"] for b in reversed(queue[1:])]
        assert popped == ["g0", "g1", "g2"]
        assert queue[0] == b"live-newer"
        assert "events:prestart:81" not in r.lists
        assert not [k for k in r.lists if ":replaying:" in k]

    def test_nothing_parked_is_a_no_op(self):
        r = _FakeRedis()
        assert engine.replay_prestart(r, [81]) == {}
        assert engine.QUEUE_KEY not in r.lists


# ── pinned envelopes ─────────────────────────────────────────────────────────

class TestPinnedEnvelope:
    def test_pinned_copy_skips_the_players_other_events(self):
        # Event 10 would match (and, with session=None, blow up recording
        # it); the copy pinned to event 81 must not touch it.
        state = engine.MatcherState(
            events={10: {"id": 10, "name": "ev", "group_id": 1,
                         "requires_confirmation": False, "submission_policy": "all",
                         "has_bingo": False, "kind": "standard", "board_size": 5,
                         "bonus_line_points": 0, "bonus_blackout_points": 0,
                         "window_start": None, "window_end": None}},
            tasks_by_event={10: [{"id": 1, "event_id": 10, "type": "item_collection",
                                  "label": "t", "target": "Verac's plateskirt",
                                  "target_value": 1, "points": 0,
                                  "requires_confirmation": False, "config": {}}]},
            participants={5: [(10, 77, None)]},
        )
        assert engine.handle_envelope(None, _FakeRedis(), state,
                                      _env(only_event_id=81)) == []
        # Control: unpinned, the same envelope does reach event 10's task.
        with pytest.raises(Exception):
            engine.handle_envelope(None, _FakeRedis(), state, _env())
