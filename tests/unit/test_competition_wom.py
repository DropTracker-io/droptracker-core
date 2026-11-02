"""Unit tests for services/competition_wom.py — competition-detail parsing,
linkability verdicts, the standings cache, and envelope emission through the
REAL group-reconciler helper (``_emit_for_row``) against a recorded
``GET /competitions/:id`` fixture. Modules are loaded by file path past the
conftest stubs (the reconciler-test recipe)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

sys.modules.setdefault("services.event_engine",
                       MagicMock(QUEUE_KEY="events:submissions",
                                 WOM_QUEUE_KEY="events:submissions:wom"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Private module names — the conftest's sys.modules stubs stay untouched for
# every other test file; the autouse fixture below routes THIS module's lazy
# imports at the real code (monkeypatch restores after each test).
recon = _load("_event_wom_reconciler_for_comp", "services/event_wom_reconciler.py")
cw = _load("_competition_wom_under_test", "services/competition_wom.py")
womutils = _load("_wiseoldman_for_comp", "utils/wiseoldman.py")


@pytest.fixture(autouse=True)
def _route_lazy_imports(monkeypatch):
    monkeypatch.setitem(sys.modules, "services.event_wom_reconciler", recon)
    stub_wom = sys.modules.get("utils.wiseoldman")
    if stub_wom is not None:
        # Additive attrs on the conftest stub: the family test the verdicts
        # need, nothing any other test reads.
        monkeypatch.setattr(stub_wom, "wom_metric_kind",
                            womutils.wom_metric_kind, raising=False)
        monkeypatch.setattr(stub_wom, "wom_skill_metric",
                            womutils.wom_skill_metric, raising=False)
    stub_models = sys.modules.get("db.models")
    if stub_models is not None:
        # The stub's attribute would silently fail tuple-membership checks.
        monkeypatch.setattr(stub_models, "COMPETITION_EVENT_KINDS",
                            ("sotw", "botw"), raising=False)


with open(os.path.join(_FIXTURES, "wom_competition_detail.json")) as fh:
    COMP_RAW = json.load(fh)


class _FakeRedis:
    def __init__(self):
        self.kv = {}
        self.pushed = []

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = str(value)

    def lpush(self, key, value):
        self.pushed.append((key, json.loads(value)))

    def expire(self, key, ttl):
        pass


# ── parsing ──────────────────────────────────────────────────────────────────

class TestParseCompetition:
    def test_normalizes_fixture(self):
        comp = cw.parse_competition(COMP_RAW)
        assert comp["id"] == 90210 and comp["title"] == "Zulrah Blitz"
        assert comp["metric"] == "zulrah" and comp["multi_metric"] is False
        assert comp["group_id"] == 141 and comp["participant_count"] == 3
        assert comp["starts_at"] == datetime(2026, 8, 20, 0, 0)
        assert comp["ends_at"] == datetime(2026, 8, 27, 0, 0)
        assert len(comp["participations"]) == 4

    def test_legacy_progress_and_modern_deltas_both_read(self):
        comp = cw.parse_competition(COMP_RAW)
        legacy = comp["participations"][0]
        assert (legacy["start"], legacy["end"], legacy["gained"]) == (250, 291, 41)
        modern = comp["participations"][1]
        assert (modern["start"], modern["end"], modern["gained"]) == (100, 180, 80)

    def test_null_progress_reads_as_none(self):
        comp = cw.parse_competition(COMP_RAW)
        empty = comp["participations"][3]
        assert empty["end"] is None and empty["gained"] == 0

    def test_garbage_is_none(self):
        assert cw.parse_competition(None) is None
        assert cw.parse_competition({"title": "no id"}) is None


# ── linkability ──────────────────────────────────────────────────────────────

class TestLinkProblems:
    NOW = datetime(2026, 8, 22, 12, 0)

    def test_fixture_links_for_botw(self):
        comp = cw.parse_competition(COMP_RAW)
        assert cw.competition_link_problems(comp, "botw", now=self.NOW) == []

    def test_metric_kind_mismatch(self):
        comp = cw.parse_competition(COMP_RAW)
        assert cw.competition_link_problems(comp, "sotw", now=self.NOW) == [
            "metric_kind_mismatch"]

    def test_team_multi_metric_and_unsupported(self):
        comp = cw.parse_competition({**COMP_RAW, "type": "team",
                                     "metrics": [{"metric": "mining"},
                                                 {"metric": "smithing"}]})
        problems = cw.competition_link_problems(comp, "sotw", now=self.NOW)
        assert "team_competition" in problems and "multi_metric" in problems
        ehb = cw.parse_competition({**COMP_RAW, "metric": "ehb", "metrics": []})
        assert "unsupported_metric" in cw.competition_link_problems(
            ehb, "botw", now=self.NOW)

    def test_finished_flagged(self):
        comp = cw.parse_competition(COMP_RAW)
        late = datetime(2026, 9, 1, 0, 0)
        assert "finished" in cw.competition_link_problems(comp, "botw", now=late)

    def test_skill_comp_for_sotw(self):
        comp = cw.parse_competition({**COMP_RAW, "metric": "mining",
                                     "metrics": [{"metric": "mining"}]})
        assert cw.competition_link_problems(comp, "sotw", now=self.NOW) == []


# ── multi-metric (multi-boss BOTW) ───────────────────────────────────────────

def _multi_raw(weights=(1, 1), metrics=("nightmare", "phosanis_nightmare")):
    player = dict(COMP_RAW["participations"][0]["player"])
    return {
        **COMP_RAW,
        "metric": metrics[0],
        "metrics": [{"metric": m, "weight": w} for m, w in zip(metrics, weights)],
        "participations": [{
            "player": player,
            "teamName": None,
            "progress": {"start": 872, "end": 918, "gained": 46},
            "deltas": [
                {"metric": "total",
                 "values": {"start": 872, "end": 918, "gained": 46}},
                {"metric": metrics[0],
                 "values": {"start": 476, "end": 476, "gained": 0}},
                {"metric": metrics[1],
                 "values": {"start": 396, "end": 442, "gained": 46}},
            ],
        }],
    }


class TestMultiMetric:
    NOW = datetime(2026, 8, 22, 12, 0)

    def test_parse_reads_per_metric_deltas(self):
        comp = cw.parse_competition(_multi_raw())
        assert comp["multi_metric"] is True
        assert comp["metrics"] == ["nightmare", "phosanis_nightmare"]
        assert comp["metric_weights"] == {"nightmare": 1.0, "phosanis_nightmare": 1.0}
        p = comp["participations"][0]
        # Ranked progress is WOM's combined total...
        assert (p["start"], p["end"], p["gained"]) == (872, 918, 46)
        # ...and the per-boss split excludes the "total" row.
        assert p["by_metric"] == {
            "nightmare": {"start": 476, "end": 476, "gained": 0},
            "phosanis_nightmare": {"start": 396, "end": 442, "gained": 46}}

    def test_multi_boss_links_for_botw_not_sotw(self):
        comp = cw.parse_competition(_multi_raw())
        assert cw.competition_link_problems(comp, "botw", now=self.NOW) == []
        assert "multi_metric" in cw.competition_link_problems(
            comp, "sotw", now=self.NOW)

    def test_weighted_metrics_refused(self):
        comp = cw.parse_competition(_multi_raw(weights=(1, 2)))
        assert "weighted_metrics" in cw.competition_link_problems(
            comp, "botw", now=self.NOW)

    def test_mixed_kinds_refused(self):
        comp = cw.parse_competition(_multi_raw(metrics=("zulrah", "mining")))
        assert "metric_kind_mismatch" in cw.competition_link_problems(
            comp, "botw", now=self.NOW)

    def test_row_credits_each_boss_never_the_total(self):
        comp = cw.parse_competition(_multi_raw())
        row = cw._participation_row(comp["participations"][0], comp["metrics"])
        assert row["data"] == [
            {"metric": "nightmare", "start": 476, "end": 476},
            {"metric": "phosanis_nightmare", "start": 396, "end": 442}]
        # A metric missing from the deltas is skipped, not given the total.
        p = dict(comp["participations"][0], by_metric={})
        assert cw._participation_row(p, comp["metrics"])["data"] == []

    def test_emits_one_wom_kc_per_boss(self):
        r = _FakeRedis()
        target = _recon_target()
        target.boss_metrics = {"nightmare", "phosanis_nightmare"}
        stats = cw._new_stats()
        comp = cw.parse_competition(_multi_raw())
        recon._emit_for_row(r, target,
                            cw._participation_row(comp["participations"][0],
                                                  comp["metrics"]),
                            clamp_epoch=None, clamp_lo=None, force=True,
                            stats=stats)
        kc = {e["data"]["boss_metric"]: (e["data"]["kc_start"], e["data"]["kc"])
              for _q, e in r.pushed}
        assert kc == {"nightmare": (476, 476), "phosanis_nightmare": (396, 442)}


class TestBossPlan:
    @pytest.fixture(autouse=True)
    def _real_boss_metric(self, monkeypatch):
        stub_wom = sys.modules.get("utils.wiseoldman")
        if stub_wom is not None:
            monkeypatch.setattr(stub_wom, "wom_boss_metric",
                                womutils.wom_boss_metric, raising=False)

    def test_splits_supported_and_unsupported(self):
        plan = cw.wom_boss_plan(["Zulrah", "Crystalline Hunllef", "The Gauntlet",
                                 "zulrah", "Definitely Not A Boss"])
        # Both Gauntlet names share one WOM metric; duplicates fold.
        assert plan["slugs"] == ["zulrah", "the_gauntlet"]
        assert plan["supported"] == ["Zulrah", "Crystalline Hunllef", "The Gauntlet"]
        assert plan["unsupported"] == ["Definitely Not A Boss"]

    def test_empty(self):
        assert cw.wom_boss_plan(None) == {"slugs": [], "supported": [],
                                         "unsupported": []}


# ── emission through the real reconciler helper ──────────────────────────────

def _recon_target():
    target = recon.ReconcileTarget(
        event_id=42,
        event_name="Zulrah Blitz",
        window_start=datetime(2026, 8, 20, 0, 0),
        window_end=datetime(2026, 8, 27, 0, 0),
        windows=[],
        wom_groups=[],
        skills={},
        boss_metrics={"zulrah"},
        effort_metrics=set(),
    )
    # Player 5 matches by wom id; player 6 by (case-folded) name.
    target.participants_by_wom[2188996] = (5, "btw fe male", None, False)
    target.participants_by_name["named match"] = (6, "Named Match", None, False)
    return target


class TestEmission:
    def test_matched_participants_emit_wom_kc_with_seeds(self):
        r = _FakeRedis()
        target, stats = _recon_target(), cw._new_stats()
        comp = cw.parse_competition(COMP_RAW)
        for p in comp["participations"]:
            if p.get("end") is None:
                continue
            recon._emit_for_row(r, target, cw._participation_row(p, "zulrah"),
                                clamp_epoch=None, clamp_lo=None, force=False,
                                stats=stats)
        by_player = {e["player_id"]: e for _q, e in r.pushed}
        assert set(by_player) == {5, 6}
        env = by_player[5]
        assert env["kind"] == "wom_kc" and env["source"] == "wom"
        assert env["data"]["boss_metric"] == "zulrah"
        assert env["data"]["kc"] == 291 and env["data"]["kc_start"] == 250
        assert env["data"]["target_event_id"] == 42
        assert env["guid"].startswith("wom:42:5:kc:zulrah:")
        assert by_player[6]["data"]["kc_start"] == 100
        assert stats["players_unmatched"] == 1  # the stranger

    def test_seen_gate_suppresses_unchanged_players(self):
        r = _FakeRedis()
        target, stats = _recon_target(), cw._new_stats()
        comp = cw.parse_competition(COMP_RAW)
        row = cw._participation_row(comp["participations"][0], "zulrah")
        recon._emit_for_row(r, target, row, clamp_epoch=None, clamp_lo=None,
                            force=False, stats=stats)
        pushed_first = len(r.pushed)
        recon._emit_for_row(r, target, row, clamp_epoch=None, clamp_lo=None,
                            force=False, stats=stats)
        assert len(r.pushed) == pushed_first
        assert stats["players_stale"] == 1
        # force=True (the final pass) re-emits regardless.
        recon._emit_for_row(r, target, row, clamp_epoch=None, clamp_lo=None,
                            force=True, stats=stats)
        assert len(r.pushed) == pushed_first + 1

    def test_ts_clamped_into_window(self):
        r = _FakeRedis()
        target, stats = _recon_target(), cw._new_stats()
        comp = cw.parse_competition(COMP_RAW)
        row = cw._participation_row(comp["participations"][0], "zulrah")
        lo = int(datetime(2026, 8, 23, 0, 0).timestamp())
        hi = int(datetime(2026, 8, 24, 0, 0).timestamp())
        recon._emit_for_row(r, target, row, clamp_epoch=hi, clamp_lo=lo,
                            force=True, stats=stats)
        assert r.pushed and lo <= r.pushed[-1][1]["ts"] <= hi


# ── standings cache ──────────────────────────────────────────────────────────

class TestStandingsCache:
    def test_all_participations_cached_with_resolution(self):
        comp = cw.parse_competition(COMP_RAW)
        cache = cw._standings_cache(comp, _recon_target())
        assert [row["display_name"] for row in cache] == [
            "Total Stranger", "Named Match", "btw fe male", "Never Updated"]
        by_name = {row["display_name"]: row for row in cache}
        assert by_name["btw fe male"]["player_id"] == 5
        assert by_name["Named Match"]["player_id"] == 6
        assert by_name["Total Stranger"]["player_id"] is None
        assert by_name["Total Stranger"]["gained"] == 390


# ── final-pass planning ──────────────────────────────────────────────────────

class TestPendingFinals:
    class _State:
        def __init__(self, events):
            self.events = events

    def test_only_closed_competition_events_pending(self):
        now = datetime(2026, 8, 27, 1, 0)
        state = self._State({
            42: {"kind": "botw", "window_end": datetime(2026, 8, 27, 0, 0)},
            43: {"kind": "botw", "window_end": datetime(2026, 8, 28, 0, 0)},
            44: {"kind": "bingo", "window_end": datetime(2026, 8, 26, 0, 0)},
            45: {"kind": "sotw", "window_end": None},
        })
        r = _FakeRedis()
        assert cw.pending_final_competition_ids(state, r, now=now) == [42]
        r.kv[cw._womcompfinal_key(42)] = "1"
        assert cw.pending_final_competition_ids(state, r, now=now) == []


# ── team competitions ────────────────────────────────────────────────────────

def _team_comp():
    raw = json.loads(json.dumps(COMP_RAW))
    raw["type"] = "team"
    names = ["Red Team", "Blue", "red team "]
    for i, p in enumerate(raw.get("participations") or []):
        p["teamName"] = names[i % len(names)]
    return raw


class TestTeamCompetitions:
    def test_parse_keeps_each_players_team(self):
        comp = cw.parse_competition(_team_comp())
        assert comp["type"] == "team"
        assert all(p["team_name"] for p in comp["participations"])
        classic = cw.parse_competition(COMP_RAW)
        assert all(p["team_name"] is None for p in classic["participations"])

    def test_team_names_are_distinct_in_first_seen_order(self):
        comp = cw.parse_competition(_team_comp())
        # "red team " folds onto "Red Team".
        assert cw.wom_team_names(comp) == ["Red Team", "Blue"]

    def test_a_team_race_needs_a_team_competition(self):
        team = cw.parse_competition(_team_comp())
        classic = cw.parse_competition(COMP_RAW)
        kind = "sotw" if womutils.wom_metric_kind(classic["metric"]) == "skill" else "botw"
        future = datetime(2000, 1, 1)
        assert "team_competition" in cw.competition_link_problems(team, kind, now=future)
        assert "classic_competition" in cw.competition_link_problems(
            classic, kind, now=future, race_format="teams")
        problems = cw.competition_link_problems(team, kind, now=future, race_format="teams")
        assert "classic_competition" not in problems and "team_competition" not in problems

    def test_the_standings_cache_carries_team_names(self):
        comp = cw.parse_competition(_team_comp())
        target = recon.ReconcileTarget(
            event_id=1, event_name="E", window_start=None, window_end=None,
            windows=[], wom_groups=[], skills={}, boss_metrics=set(),
            effort_metrics=set())
        rows = cw._standings_cache(comp, target)
        assert {r["team_name"] for r in rows} <= {"Red Team", "Blue", "red team"}
        assert all(r["team_name"] for r in rows)


class TestWomTeamPayload:
    def test_shape_clipping_and_empty_teams(self):
        payload = womutils.wom_team_payload([
            ("A team name that is far longer than thirty characters", ["b", "A", "a", " "]),
            ("Empty", []),
            ("  ", ["x"]),
            ("Blue", ["Zed", "alpha"]),
        ])
        assert payload == [
            {"name": "A team name that is far longer",
             "participants": ["A", "a", "b"]},
            {"name": "Blue", "participants": ["alpha", "Zed"]},
        ]
        assert len(payload[0]["name"]) == womutils.WOM_TEAM_NAME_MAX
