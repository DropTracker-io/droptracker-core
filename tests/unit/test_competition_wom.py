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
                                     "metrics": [{"metric": "zulrah"},
                                                 {"metric": "vorkath"}]})
        problems = cw.competition_link_problems(comp, "botw", now=self.NOW)
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
