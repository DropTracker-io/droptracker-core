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
from datetime import datetime, timedelta
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
        self.hashes = {}
        self.pushed = []

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = str(value)

    def lpush(self, key, value):
        self.pushed.append(json.loads(value))

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update({str(k): str(v) for k, v in mapping.items()})
        if field is not None:
            h[str(field)] = str(value)

    def expire(self, key, ttl):
        pass


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


def _cands(target, rows, now, *, activity=None, active_interval=3600,
           flat=None, redis=None):
    latest = recon._latest_row_by_participant(target, rows)
    if activity is None:
        activity = recon._observe_activity(redis or _FakeRedis(), target, latest)
    return recon._select_update_candidates(
        target, latest, activity, now,
        active_interval=active_interval, flat_min_stale=flat)


class TestUpdateRotation:
    def test_interval_scales_with_active_roster(self):
        assert recon._active_interval(1) == recon.WOM_EVENT_UPDATE_MIN_INTERVAL
        assert recon._active_interval(4) == recon.WOM_EVENT_UPDATE_MIN_INTERVAL
        assert recon._active_interval(100) == 100 * recon.WOM_EVENT_UPDATE_SECONDS_PER_PLAYER
        assert recon._active_interval(1000) == recon.WOM_EVENT_UPDATE_MAX_INTERVAL

    def test_idle_interval_backoff(self):
        base = recon.WOM_EVENT_IDLE_BASE_INTERVAL
        assert recon._idle_interval(0, 1800) == 1800  # active cadence
        assert recon._idle_interval(1, 1800) == base
        assert recon._idle_interval(3, 1800) == base * 4
        assert recon._idle_interval(50, 1800) == recon.WOM_EVENT_IDLE_MAX_INTERVAL
        # Never faster than the active cadence.
        assert recon._idle_interval(1, 7200) == 7200

    def test_candidates_missing_from_rows_sort_stalest(self):
        # Player 902 has no snapshot inside the window (absent from rows) —
        # maximally stale, sorts ahead of 901 who was seen recently.
        target = _target(participants_by_wom={
            2188996: (901, "btw fe male", datetime(2026, 6, 30), False),
            555555: (902, "never updated", datetime(2026, 6, 30), False),
        })
        now = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"]) + 10_000_000
        cands = _cands(target, [ROW_RANKED], now)
        assert [c[2] for c in cands] == [902, 901]
        assert cands[0][3] == "never updated"  # falls back to DT player name
        assert cands[1][3] == ROW_RANKED["player"]["username"]

    def test_fresh_players_not_candidates(self):
        target = _target()
        now = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"]) + 60
        assert _cands(target, [ROW_RANKED], now) == []

    def test_stubs_sort_first(self):
        target = _target(participants_by_wom={
            2188996: (901, "btw fe male", datetime(2026, 6, 30), False),
            555555: (902, "wom stub", datetime(2026, 6, 30), True),
        })
        now = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"]) + 10_000_000
        cands = _cands(target, [ROW_RANKED], now)
        assert [c[2] for c in cands] == [902, 901]
        assert cands[0][0] == 0  # stub priority tier


class TestActivityTiering:
    UPDATED = "player", "updatedAt"

    def _row_at(self, epoch_offset, *, zulrah_end=250):
        row = json.loads(json.dumps(ROW_RANKED))
        base = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"])
        row["player"]["updatedAt"] = datetime.utcfromtimestamp(
            base + epoch_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for m in row["data"]:
            if m["metric"] == "zulrah":
                m["end"] = zulrah_end
        return row

    def test_progress_resets_streak_no_progress_increments(self):
        target = _target()
        r = _FakeRedis()
        # First observation: no prior entry -> streak 0 (active).
        act = recon._observe_activity(
            r, target, recon._latest_row_by_participant(target, [self._row_at(0)]))
        assert act[901][1] == 0
        # New snapshot, no relevant movement -> idle streak 1, then 2.
        act = recon._observe_activity(
            r, target, recon._latest_row_by_participant(target, [self._row_at(100)]))
        assert act[901][1] == 1
        act = recon._observe_activity(
            r, target, recon._latest_row_by_participant(target, [self._row_at(200)]))
        assert act[901][1] == 2
        # New snapshot with a zulrah kill -> active again.
        act = recon._observe_activity(
            r, target,
            recon._latest_row_by_participant(target, [self._row_at(300, zulrah_end=251)]))
        assert act[901][1] == 0

    def test_same_snapshot_does_not_touch_streak(self):
        target = _target()
        r = _FakeRedis()
        latest = recon._latest_row_by_participant(target, [self._row_at(0)])
        recon._observe_activity(r, target, latest)
        act = recon._observe_activity(r, target, latest)  # same updatedAt
        assert act[901][1] == 0

    def test_idle_players_wait_for_backoff(self):
        target = _target()
        r = _FakeRedis()
        base = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"])
        recon._observe_activity(
            r, target, recon._latest_row_by_participant(target, [self._row_at(0)]))
        act = recon._observe_activity(
            r, target, recon._latest_row_by_participant(target, [self._row_at(100)]))
        assert act[901][1] == 1  # idle: due after IDLE_BASE, not active interval
        rows = [self._row_at(100)]
        now = base + 100 + 1900  # past a 1800s active interval, inside 3600 backoff
        assert _cands(target, rows, now, activity=act, active_interval=1800) == []
        now = base + 100 + recon.WOM_EVENT_IDLE_BASE_INTERVAL + 10
        cands = _cands(target, rows, now, activity=act, active_interval=1800)
        assert [c[2] for c in cands] == [901]
        assert cands[0][0] == 2  # idle priority tier sorts after active

    def test_flat_mode_ignores_streaks(self):
        target = _target()
        r = _FakeRedis()
        base = recon._parse_wom_ts(ROW_RANKED["player"]["updatedAt"])
        recon._observe_activity(
            r, target, recon._latest_row_by_participant(target, [self._row_at(0)]))
        act = recon._observe_activity(
            r, target, recon._latest_row_by_participant(target, [self._row_at(100)]))
        rows = [self._row_at(100)]
        now = base + 100 + 1900
        cands = _cands(target, rows, now, activity=act, flat=1800)
        assert [c[2] for c in cands] == [901]


class TestRequestUpdates:
    ACT_KEY = "events:42:womact"

    def _run(self, candidates, results, max_updates=40, *, redis=None,
             activity=None, latest=None):
        import asyncio
        from unittest.mock import AsyncMock, patch
        r = redis or _FakeRedis()
        activity = activity if activity is not None else {}
        # Default: every candidate has a bulk-gained row (in the WOM group),
        # so successes don't take the outside-the-group streak bump.
        if latest is None:
            latest = {c[2]: (c[1], c[3], {}) for c in candidates}
        mock = AsyncMock(side_effect=results)
        with patch.object(womutils, "request_player_update", mock):
            # _request_updates lazily imports from utils.wiseoldman
            sys.modules["utils.wiseoldman"] = womutils
            n = asyncio.run(recon._request_updates(
                r, candidates, max_updates=max_updates, act_key=self.ACT_KEY,
                activity=activity, latest=latest, active_interval=1800))
        return n, r, mock

    @staticmethod
    def _cand(pid, cooldown=600):
        return (1, 0, pid, f"p{pid}", cooldown)

    def test_respects_cap_and_sets_cooldowns(self):
        cands = [self._cand(pid) for pid in range(1, 6)]
        n, r, mock = self._run(cands, [True] * 5, max_updates=3)
        assert n == 3 and mock.await_count == 3
        assert r.kv.get("wom:eventupd:1") and r.kv.get("wom:eventupd:3")
        assert "wom:eventupd:4" not in r.kv

    def test_cooldown_skips_without_spending(self):
        cands = [self._cand(1), self._cand(2)]
        r = _FakeRedis()
        r.kv["wom:eventupd:1"] = "1"
        n, _r, mock = self._run(cands, [True], redis=r)
        assert n == 1 and mock.await_count == 1

    def test_three_consecutive_failures_abort_batch(self):
        cands = [self._cand(pid) for pid in range(1, 8)]
        n, _r, mock = self._run(cands, [False, False, False, True, True])
        assert n == 0 and mock.await_count == 3

    def test_failures_reset_on_success(self):
        cands = [self._cand(pid) for pid in range(1, 8)]
        n, _r, mock = self._run(cands, [False, False, True, False, False, True, False])
        assert n == 2 and mock.await_count == 7

    def test_failure_bumps_streak_in_ledger(self):
        cands = [self._cand(1)]
        activity = {1: (100, 0, "abc")}
        n, r, _mock = self._run(cands, [False], activity=activity)
        assert n == 0
        assert activity[1] == (100, 1, "abc")
        assert r.hashes[self.ACT_KEY]["1"] == "100:1:abc"

    def test_success_outside_wom_group_bumps_streak(self):
        # Successful update but the player never appears in bulk-gained rows
        # (not in the WOM group): decay them instead of active-cadence forever.
        cands = [self._cand(7)]
        n, r, _mock = self._run(cands, [True], latest={})
        assert n == 1
        assert r.hashes[self.ACT_KEY]["7"] == "0:1:"

    def test_success_in_group_leaves_ledger_alone(self):
        cands = [self._cand(1)]
        activity = {1: (100, 0, "abc")}
        n, r, _mock = self._run(cands, [True], activity=activity)
        assert n == 1
        assert activity[1] == (100, 0, "abc")
        assert self.ACT_KEY not in r.hashes


# ── recurring-schedule fetch bounds (web82a) ─────────────────────────────────

class TestScoringBounds:
    """WOM reports GAINS BETWEEN TWO INSTANTS, so a fetch span that crosses a
    closed period would hand the matcher XP/KC earned while the event was
    paused. Every fetch is therefore bounded to a single scoring window.
    """

    WEEKENDS = [(datetime(2026, 8, 1), datetime(2026, 8, 3)),
                (datetime(2026, 8, 8), datetime(2026, 8, 10))]

    def _target(self, windows=None):
        return recon.ReconcileTarget(
            event_id=1, event_name="e",
            window_start=datetime(2026, 8, 1), window_end=datetime(2026, 9, 1),
            windows=list(windows if windows is not None else self.WEEKENDS))

    def test_continuous_event_uses_the_whole_event_window(self):
        bounds = recon.scoring_bounds(self._target(windows=[]), datetime(2026, 8, 5))
        assert bounds == (datetime(2026, 8, 1), datetime(2026, 8, 5))

    def test_inside_a_window_fetches_from_that_window_start(self):
        bounds = recon.scoring_bounds(self._target(), datetime(2026, 8, 2, 12))
        assert bounds == (datetime(2026, 8, 1), datetime(2026, 8, 2, 12))

    def test_between_windows_fetches_nothing(self):
        assert recon.scoring_bounds(self._target(), datetime(2026, 8, 5)) is None

    def test_grace_period_captures_a_window_tail(self):
        # WOM snapshots lag the game: without this the last minutes of every
        # weekend would be lost the instant the window shut.
        just_closed = datetime(2026, 8, 3) + timedelta(minutes=10)
        assert recon.scoring_bounds(self._target(), just_closed) == self.WEEKENDS[0]

    def test_grace_expires(self):
        long_after = datetime(2026, 8, 3) + timedelta(
            seconds=recon.WINDOW_TAIL_GRACE_SECONDS + 60)
        assert recon.scoring_bounds(self._target(), long_after) is None

    def test_final_pass_falls_back_to_the_last_elapsed_window(self):
        bounds = recon.scoring_bounds(self._target(), datetime(2026, 9, 1), final=True)
        assert bounds == self.WEEKENDS[-1]

    def test_no_window_start_means_no_fetch(self):
        target = self._target()
        target.window_start = None
        assert recon.scoring_bounds(target, datetime(2026, 8, 2)) is None


class TestClosingEdges:
    def test_continuous_event_has_only_the_event_end(self):
        target = recon.ReconcileTarget(
            event_id=1, event_name="e", window_start=datetime(2026, 8, 1),
            window_end=datetime(2026, 9, 1))
        assert recon._closing_edges(target) == [("end", datetime(2026, 9, 1))]

    def test_each_scoring_window_gets_its_own_pre_close_pass(self):
        # A weekend's final hours are as final as the event's — the next window
        # re-baselines, so gains not fetched before the close are lost.
        target = recon.ReconcileTarget(
            event_id=1, event_name="e", window_start=datetime(2026, 8, 1),
            window_end=datetime(2026, 9, 1),
            windows=[(datetime(2026, 8, 1), datetime(2026, 8, 3)),
                     (datetime(2026, 8, 8), datetime(2026, 8, 10))])
        assert recon._closing_edges(target) == [
            ("w0", datetime(2026, 8, 3)),
            ("w1", datetime(2026, 8, 10)),
            ("end", datetime(2026, 9, 1)),
        ]
