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


def _plugin(r, kc, **kw):
    """A drop's loot-tracker kill_count (the first one credits the kill)."""
    return _fold(r, kc, first_credit_offset=1,
                 source=engine.KC_SOURCE_PLUGIN, **kw)


def _wom(r, kc, **kw):
    """A WOM reconciler envelope's boss-metric KC."""
    return _fold(r, kc, source=engine.KC_SOURCE_WOM, **kw)


def _redis_credited(r):
    """Total the scope has handed out across both sources."""
    return engine._redis_int(r, engine._kc_credited_key(EID, TID, PID))


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

    def test_wom_window_seed_tops_up_what_the_plugin_baseline_missed(self):
        # The plugin's baseline is lazy — it starts at the first drop it sees,
        # so kills earlier in the window are invisible to it. WOM's window-start
        # seed DOES span them, and its estimate is the one that counts.
        r = _FakeRedis()
        assert _plugin(r, 258) == 1            # one kill observed live
        assert _wom(r, 255, seed=240) == 14    # 15 in-window kills, 1 paid
        assert _wom(r, 260) == 5

    def test_wom_ahead_then_plugin_starts_reporting(self):
        r = _FakeRedis()
        assert _wom(r, 250, seed=240) == 10    # WOM credited the window
        # The plugin's first report inherits that estimate rather than
        # re-crediting from its own counter: nothing new to pay out.
        assert _plugin(r, 249) == 0
        assert _plugin(r, 251) == 2            # and then tracks live from there

    def test_neither_source_re_credits_the_others_kills(self):
        r = _FakeRedis()
        assert _plugin(r, 100) == 1
        assert _plugin(r, 110) == 10
        assert _wom(r, 4_000, seed=3_989) == 0  # same 11 kills, other counter


class TestMismatchedCounterSemantics:
    """Bug report #131. WOM's ``sol_heredit`` counts COMPLETED Colosseum runs;
    the plugin's Fortis Colosseum chest KC counts every attempt. One player's
    counters read 172 and 920 on the same day, and the old shared watermark
    credited the 748-kill difference as in-event effort."""

    def test_lifetime_difference_is_never_credited(self):
        r = _FakeRedis()
        assert _wom(r, 172, seed=170) == 2     # 2 completed runs this window
        # The first chest arrives with a counter on a completely different
        # scale. It must credit its own progress, not the 748-kill gap.
        assert _plugin(r, 920) == 0
        assert _plugin(r, 921) == 1
        assert _plugin(r, 925) == 4

    def test_mismatch_in_the_other_arrival_order(self):
        r = _FakeRedis()
        assert _plugin(r, 920) == 1
        assert _wom(r, 172, seed=170) <= 2     # never 172 - 920's scale
        _plugin(r, 930)
        # 11 chests opened (920..930), and that is the whole bill.
        assert _redis_credited(r) == 11

    def test_high_scale_source_still_leads_after_the_low_one(self):
        # 11 chests opened in the window is 11 units of effort even though WOM
        # only ever confirms the 8 that were completed.
        r = _FakeRedis()
        _wom(r, 172, seed=172)
        for kc in range(921, 932):
            _plugin(r, kc)
        assert _redis_credited(r) == 11
        assert _wom(r, 180) == 0               # 8 completions < 11 attempts


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
