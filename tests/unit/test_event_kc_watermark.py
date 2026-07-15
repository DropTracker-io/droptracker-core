"""Unit tests for the event engine's absolute-value watermark folds
(services/event_engine.py): the KC watermark shared by plugin drops and WOM
reconciler envelopes, and the WOM window-start seeding added to the XP fold.

Loaded by file path like test_event_engine_matcher.py so the conftest
sys.modules stubs never interfere.
"""

import importlib.util
import os
import sys

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_watermark_test", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_watermark_test"] = engine
_spec.loader.exec_module(engine)


class _FakeRedis:
    def __init__(self):
        self.sets = {}
        self.kv = {}

    def sadd(self, key, member):
        s = self.sets.setdefault(key, set())
        if member in s:
            return 0
        s.add(member)
        return 1

    def smembers(self, key):
        return self.sets.get(key, set())

    def expire(self, key, ttl):
        return True

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = str(value)

    def delete(self, key):
        self.kv.pop(key, None)
        self.sets.pop(key, None)

    def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key) or 0) + 1)
        return int(self.kv[key])


EID, TID, PID = 2, 9, 5


def _fold(r, kc, **kw):
    return engine._fold_kc_watermark(r, EID, TID, PID, kc, **kw)


class TestKcWatermarkPlugin:
    def test_first_drop_credits_one(self):
        r = _FakeRedis()
        assert _fold(r, 100, first_credit_offset=1) == 1

    def test_same_kc_replay_credits_zero(self):
        r = _FakeRedis()
        _fold(r, 100, first_credit_offset=1)
        assert _fold(r, 100, first_credit_offset=1) == 0

    def test_gap_between_drops_credits_gap(self):
        # Kills whose loot submissions were lost still count once a newer
        # absolute KC arrives.
        r = _FakeRedis()
        _fold(r, 500, first_credit_offset=1)
        assert _fold(r, 503, first_credit_offset=1) == 3

    def test_regressed_kc_credits_zero_and_keeps_watermark(self):
        r = _FakeRedis()
        _fold(r, 103, first_credit_offset=1)
        assert _fold(r, 100) == 0
        assert _fold(r, 104) == 1  # measured from 103, not 100

    def test_invalid_kc_credits_zero(self):
        r = _FakeRedis()
        assert _fold(r, None) == 0
        assert _fold(r, "nan") == 0
        assert _fold(r, 0) == 0


class TestKcWatermarkWom:
    def test_wom_first_with_window_seed_credits_gained(self):
        r = _FakeRedis()
        assert _fold(r, 250, seed=240) == 10

    def test_wom_first_without_seed_credits_zero(self):
        r = _FakeRedis()
        assert _fold(r, 250) == 0
        assert _fold(r, 252) == 2  # lazy baseline established at 250

    def test_seed_above_current_ignored(self):
        r = _FakeRedis()
        assert _fold(r, 250, seed=260) == 0

    def test_plugin_ahead_wom_folds_to_zero(self):
        r = _FakeRedis()
        _fold(r, 258, first_credit_offset=1)  # plugin drop
        assert _fold(r, 255, seed=240) == 0   # stale WOM snapshot
        assert _fold(r, 260) == 2             # newer WOM tops up past 258

    def test_wom_ahead_plugin_folds_to_zero(self):
        r = _FakeRedis()
        assert _fold(r, 250, seed=240) == 10  # WOM credited the window
        assert _fold(r, 249, first_credit_offset=1) == 0  # older plugin drop
        assert _fold(r, 251, first_credit_offset=1) == 1


class TestKcFallbackCounter:
    def test_fallback_credits_subtracted_from_next_absolute_fold(self):
        r = _FakeRedis()
        _fold(r, 100, first_credit_offset=1)
        # Two kills credited via the no-kill_count cooldown path:
        r.incr(engine._kc_fallback_key(EID, TID, PID))
        r.incr(engine._kc_fallback_key(EID, TID, PID))
        # Absolute KC later covers those same kills (100 -> 103 = 3 kills,
        # 2 already credited).
        assert _fold(r, 103) == 1
        assert r.get(engine._kc_fallback_key(EID, TID, PID)) is None

    def test_partial_fallback_consumption_carries_remainder(self):
        r = _FakeRedis()
        _fold(r, 100, first_credit_offset=1)
        for _ in range(3):
            r.incr(engine._kc_fallback_key(EID, TID, PID))
        assert _fold(r, 102) == 0  # delta 2 fully consumed by fallback
        assert r.get(engine._kc_fallback_key(EID, TID, PID)) == "1"
        assert _fold(r, 104) == 1  # delta 2, remaining fallback 1 consumed


class TestLegacyKcdedupeTransition:
    def _seed_legacy(self, r, kc):
        r.sadd(f"events:{EID}:kcdedupe:{TID}:{PID}", f"zulrah:{kc}")

    def test_plugin_first_fold_respects_legacy_max(self):
        # Mid-event deploy: kills up to KC 120 were credited via kcdedupe.
        r = _FakeRedis()
        self._seed_legacy(r, 119)
        self._seed_legacy(r, 120)
        assert _fold(r, 121, first_credit_offset=1) == 1  # not 1 per legacy kill

    def test_wom_seed_cannot_recredit_legacy_kills(self):
        r = _FakeRedis()
        self._seed_legacy(r, 120)
        # WOM window seed says 110 -> 125, but 111..120 were already credited.
        assert _fold(r, 125, seed=110) == 5

    def test_malformed_legacy_members_ignored(self):
        r = _FakeRedis()
        r.sadd(f"events:{EID}:kcdedupe:{TID}:{PID}", "garbage")
        assert _fold(r, 50, first_credit_offset=1) == 1


class TestXpBaselineSeed:
    def test_unset_baseline_with_seed_credits_window_gain(self):
        r = _FakeRedis()
        assert engine._fold_xp_baseline(r, EID, PID, "attack", 1_050_000,
                                        seed=1_000_000) == 50_000

    def test_seed_only_applies_on_first_fold(self):
        r = _FakeRedis()
        engine._fold_xp_baseline(r, EID, PID, "attack", 1_000_000)
        assert engine._fold_xp_baseline(r, EID, PID, "attack", 1_050_000,
                                        seed=900_000) == 50_000

    def test_invalid_seed_falls_back_to_lazy_baseline(self):
        r = _FakeRedis()
        assert engine._fold_xp_baseline(r, EID, PID, "attack", 1000, seed=0) == 0
        assert engine._fold_xp_baseline(r, EID, PID, "magic", 1000, seed=2000) == 0
        assert engine._fold_xp_baseline(r, EID, PID, "prayer", 1000, seed="x") == 0

    def test_no_seed_keeps_existing_behavior(self):
        r = _FakeRedis()
        assert engine._fold_xp_baseline(r, EID, PID, "attack", 1000) == 0
        assert engine._fold_xp_baseline(r, EID, PID, "attack", 1500) == 500
        assert engine._fold_xp_baseline(r, EID, PID, "attack", 1400) == 0


class TestSeedAllowed:
    def test_rules(self):
        from datetime import datetime
        early = datetime(2026, 7, 1)
        start = datetime(2026, 7, 2)
        late = datetime(2026, 7, 3)
        assert engine._seed_allowed(None, start) is True
        assert engine._seed_allowed(early, None) is True
        assert engine._seed_allowed(early, start) is True
        assert engine._seed_allowed(start, start) is True
        assert engine._seed_allowed(late, start) is False
