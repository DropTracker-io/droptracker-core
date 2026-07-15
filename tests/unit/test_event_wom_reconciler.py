"""Unit tests for services/event_wom_reconciler.py envelope building (from
recorded bulk-gained fixtures) and utils/wiseoldman.py metric mapping.

Both modules are loaded by file path so the conftest sys.modules stubs don't
interfere; the reconciler's lazy ``from services.event_engine import
QUEUE_KEY`` is satisfied with a stub queue name (nothing in the unit suite
imports the real module under that dotted name).
"""

import importlib.util
import json
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

sys.modules.setdefault("services.event_engine",
                       MagicMock(QUEUE_KEY="events:submissions"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


recon = _load("_event_wom_reconciler_test", "services/event_wom_reconciler.py")
womutils = _load("_wiseoldman_under_test", "utils/wiseoldman.py")


with open(os.path.join(_FIXTURES, "wom_bulk_gained.json")) as fh:
    BULK_GAINED = json.load(fh)

# Row 0: WOM player 2188996 "btw fe male" — zulrah start=250 end=250,
# updatedAt/endDate 2026-07-14T13:35:36Z. Row 1: WOM player 399504 —
# zulrah unranked (-1).
ROW_RANKED, ROW_UNRANKED = BULK_GAINED[0], BULK_GAINED[1]


class _FakeRedis:
    def __init__(self):
        self.kv = {}
        self.pushed = []

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = str(value)

    def lpush(self, key, value):
        self.pushed.append(json.loads(value))


def _target(**kw):
    base = dict(
        event_id=42,
        event_name="test event",
        window_start=datetime(2026, 7, 1),
        window_end=datetime(2026, 7, 20),
        wom_groups=[(5, 10713, None)],
        skills={"attack": "attack", "runecraft": "runecrafting"},
        boss_metrics={"zulrah"},
        participants_by_wom={
            2188996: (901, "btw fe male", datetime(2026, 6, 30), False),
        },
        participants_by_name={},
    )
    base.update(kw)
    return recon.ReconcileTarget(**base)


def _emit(target, row, *, clamp=None, force=False):
    r = _FakeRedis()
    stats = recon._new_stats()
    pushed = recon._emit_for_row(r, target, row, clamp_epoch=clamp,
                                 force=force, stats=stats)
    return r, stats, pushed


class TestEmitForRow:
    def test_emits_xp_and_kc_envelopes(self):
        r, stats, pushed = _emit(_target(), ROW_RANKED)
        assert pushed == 3  # attack + runecraft + zulrah
        kinds = sorted(e["kind"] for e in r.pushed)
        assert kinds == ["experience", "experience", "wom_kc"]
        by_skill = {e["data"].get("skill"): e for e in r.pushed
                    if e["kind"] == "experience"}
        metrics = {m["metric"]: m for m in ROW_RANKED["data"]}
        atk = by_skill["attack"]
        assert atk["player_id"] == 901
        assert atk["source"] == "wom" and atk["used_api"] is True
        assert atk["data"]["xp"] == metrics["attack"]["end"]
        assert atk["data"]["xp_start"] == metrics["attack"]["start"]
        assert atk["data"]["target_event_id"] == 42
        assert atk["data"]["level"] == 99  # 13.2M attack xp
        # DT skill key rides in the envelope; WOM slug only used for lookup.
        rc = by_skill["runecraft"]
        assert rc["data"]["xp"] == metrics["runecrafting"]["end"]
        kc = next(e for e in r.pushed if e["kind"] == "wom_kc")
        assert kc["data"]["boss_metric"] == "zulrah"
        assert kc["data"]["kc"] == 250 and kc["data"]["kc_start"] == 250
        # ts = the player's last snapshot inside the window (row endDate).
        assert atk["ts"] == recon._parse_wom_ts(ROW_RANKED["endDate"])
        assert atk["guid"] == f"wom:42:901:attack:{atk['ts']}"
        assert kc["guid"] == f"wom:42:901:kc:zulrah:{kc['ts']}"

    def test_unranked_metric_skipped(self):
        target = _target(participants_by_wom={
            399504: (902, "1b exp 1", datetime(2026, 6, 30), False)})
        r, _stats, _ = _emit(target, ROW_UNRANKED)
        assert not any(e["kind"] == "wom_kc" for e in r.pushed)  # zulrah = -1
        assert any(e["kind"] == "experience" for e in r.pushed)

    def test_unmatched_player_counted_and_skipped(self):
        target = _target(participants_by_wom={})
        r, stats, pushed = _emit(target, ROW_RANKED)
        assert pushed == 0 and r.pushed == []
        assert stats["players_unmatched"] == 1

    def test_name_fallback_matches_display_name(self):
        target = _target(
            participants_by_wom={},
            participants_by_name={
                "btw fe male": (901, "btw fe male", datetime(2026, 6, 30), False)})
        _r, stats, pushed = _emit(target, ROW_RANKED)
        assert pushed == 3 and stats["players_emitted"] == 1

    def test_womseen_gate_skips_unchanged_players(self):
        target = _target()
        r = _FakeRedis()
        stats = recon._new_stats()
        assert recon._emit_for_row(r, target, ROW_RANKED, clamp_epoch=None,
                                   force=False, stats=stats) == 3
        # Same snapshot again -> gated.
        stats2 = recon._new_stats()
        assert recon._emit_for_row(r, target, ROW_RANKED, clamp_epoch=None,
                                   force=False, stats=stats2) == 0
        assert stats2["players_stale"] == 1

    def test_force_bypasses_womseen_gate(self):
        target = _target()
        r = _FakeRedis()
        stats = recon._new_stats()
        recon._emit_for_row(r, target, ROW_RANKED, clamp_epoch=None,
                            force=False, stats=stats)
        assert recon._emit_for_row(r, target, ROW_RANKED, clamp_epoch=None,
                                   force=True, stats=stats) == 3

    def test_final_pass_clamps_ts_to_window_end(self):
        clamp = recon._parse_wom_ts("2026-07-10T00:00:00.000Z")
        r, _stats, _ = _emit(_target(), ROW_RANKED, clamp=clamp)
        assert all(e["ts"] == clamp for e in r.pushed)
        assert r.pushed[0]["guid"].endswith(str(clamp))


class TestHelpers:
    def test_level_for_xp_known_values(self):
        assert recon._level_for_xp(0) == 1
        assert recon._level_for_xp(83) == 2
        assert recon._level_for_xp(13_034_430) == 98
        assert recon._level_for_xp(13_034_431) == 99
        assert recon._level_for_xp(200_000_000) == 99

    def test_parse_wom_ts(self):
        assert recon._parse_wom_ts(None) is None
        assert recon._parse_wom_ts("garbage") is None
        epoch = recon._parse_wom_ts("2026-07-14T13:35:36.693Z")
        assert isinstance(epoch, int) and epoch > 1_780_000_000


class TestMetricMapping:
    def test_skill_mapping(self):
        assert womutils.wom_skill_metric("runecraft") == "runecrafting"
        assert womutils.wom_skill_metric("Attack") == "attack"
        assert womutils.wom_skill_metric("sailing") == "sailing"
        assert womutils.wom_skill_metric("not a skill") is None

    def test_boss_mapping_normalization_and_overrides(self):
        cases = {
            "Zulrah": "zulrah",
            "Theatre of Blood: Hard Mode": "theatre_of_blood_hard_mode",
            "Chambers of Xeric: Challenge Mode": "chambers_of_xeric_challenge_mode",
            "Tombs of Amascut: Expert Mode": "tombs_of_amascut_expert",
            "K'ril Tsutsaroth": "kril_tsutsaroth",
            "Kree'arra": "kreearra",
            "TzTok-Jad": "tztok_jad",
            "Barrows": "barrows_chests",
            "The Nightmare": "nightmare",
            "Phosani's Nightmare": "phosanis_nightmare",
            "Corrupted Hunllef": "the_corrupted_gauntlet",
            "The Whisperer": "the_whisperer",
            "Vet'ion": "vetion",
            "Yama": "yama",
            "Made Up Boss": None,
        }
        for name, slug in cases.items():
            assert womutils.wom_boss_metric(name) == slug, name

    def test_metric_families_do_not_cross(self):
        # A kc_target named like a skill must not resolve onto the skill
        # metric (bulk-gained rows carry both families in one list).
        assert womutils.wom_boss_metric("attack") is None
        assert womutils.wom_skill_metric("zulrah") is None


class TestUpdateRotation:
    def test_interval_scales_with_roster(self):
        assert recon._update_interval(1) == recon.WOM_EVENT_UPDATE_MIN_INTERVAL
        assert recon._update_interval(4) == recon.WOM_EVENT_UPDATE_MIN_INTERVAL
        assert recon._update_interval(100) == 100 * recon.WOM_EVENT_UPDATE_SECONDS_PER_PLAYER
        assert recon._update_interval(1000) == recon.WOM_EVENT_UPDATE_MAX_INTERVAL

    def test_candidates_missing_from_rows_sort_stalest(self):
        # Player 902 has no snapshot inside the window (absent from rows) —
        # maximally stale, sorts ahead of 901 who was seen recently.
        target = _target(participants_by_wom={
            2188996: (901, "btw fe male", datetime(2026, 6, 30), False),
            555555: (902, "never updated", datetime(2026, 6, 30), False),
        })
        now = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"]) + 10_000_000
        cands = recon._select_update_candidates(target, [ROW_RANKED], now, 3600)
        assert [c[2] for c in cands] == [902, 901]
        assert cands[0][3] == "never updated"  # falls back to DT player name
        assert cands[1][3] == ROW_RANKED["player"]["username"]

    def test_fresh_players_not_candidates(self):
        target = _target()
        now = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"]) + 60
        assert recon._select_update_candidates(target, [ROW_RANKED], now, 3600) == []

    def test_stubs_sort_first(self):
        target = _target(participants_by_wom={
            2188996: (901, "btw fe male", datetime(2026, 6, 30), False),
            555555: (902, "wom stub", datetime(2026, 6, 30), True),
        })
        now = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"]) + 10_000_000
        cands = recon._select_update_candidates(target, [ROW_RANKED], now, 3600)
        assert [c[2] for c in cands] == [902, 901]
        assert cands[0][0] == 0  # stub priority tier


class TestRequestUpdates:
    def _run(self, candidates, results, max_updates=40):
        import asyncio
        from unittest.mock import AsyncMock, patch
        r = _FakeRedis()
        mock = AsyncMock(side_effect=results)
        with patch.object(womutils, "request_player_update", mock):
            # _request_updates lazily imports from utils.wiseoldman
            sys.modules["utils.wiseoldman"] = womutils
            try:
                n = asyncio.run(recon._request_updates(
                    r, candidates, max_updates=max_updates, success_cooldown=600))
            finally:
                pass
        return n, r, mock

    def test_respects_cap_and_sets_cooldowns(self):
        cands = [(1, 0, pid, f"p{pid}") for pid in range(1, 6)]
        n, r, mock = self._run(cands, [True] * 5, max_updates=3)
        assert n == 3 and mock.await_count == 3
        assert r.kv.get("wom:eventupd:1") and r.kv.get("wom:eventupd:3")
        assert "wom:eventupd:4" not in r.kv

    def test_cooldown_skips_without_spending(self):
        cands = [(1, 0, 1, "p1"), (1, 0, 2, "p2")]
        r = _FakeRedis()
        r.kv["wom:eventupd:1"] = "1"
        import asyncio
        from unittest.mock import AsyncMock, patch
        mock = AsyncMock(return_value=True)
        with patch.object(womutils, "request_player_update", mock):
            sys.modules["utils.wiseoldman"] = womutils
            n = asyncio.run(recon._request_updates(
                r, cands, max_updates=10, success_cooldown=600))
        assert n == 1 and mock.await_count == 1

    def test_three_consecutive_failures_abort_batch(self):
        cands = [(1, 0, pid, f"p{pid}") for pid in range(1, 8)]
        n, _r, mock = self._run(cands, [False, False, False, True, True])
        assert n == 0 and mock.await_count == 3

    def test_failures_reset_on_success(self):
        cands = [(1, 0, pid, f"p{pid}") for pid in range(1, 8)]
        n, _r, mock = self._run(cands, [False, False, True, False, False, True, False])
        assert n == 2 and mock.await_count == 7
