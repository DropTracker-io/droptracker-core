"""Unit tests for ``validated_competition_config`` — the event routes'
validator for the SOTW/BOTW wizard block — plus the managed-task guard in
``validate_task_payload``. NPC/skill canonicalization is monkeypatched to a
fixed known set (the test_event_task_validation conventions); the conftest
already stubs ``utils.wiseoldman``.
"""
from __future__ import annotations

import sys

import pytest

import web_api.routes.event_task_validation as etv
from web_api.common import ProblemException

KNOWN_NPCS = {"Zulrah", "Vorkath", "Dagannoth Rex"}
_NPC_BY_NORM = {n.lower(): n for n in KNOWN_NPCS}
KNOWN_SKILLS = {"mining", "fishing", "attack"}


@pytest.fixture(autouse=True)
def _stub_lookups(monkeypatch):
    monkeypatch.setattr(
        etv, "_canonical_npc",
        lambda s, name: _NPC_BY_NORM.get((name or "").strip().lower()),
    )
    monkeypatch.setattr(etv, "expand_source_names", lambda name: [name])
    # The conftest stubs utils.wiseoldman; pin the metric resolver's contract.
    monkeypatch.setattr(
        sys.modules["utils.wiseoldman"], "wom_skill_metric",
        lambda key: key if key in KNOWN_SKILLS else None,
        raising=False,
    )


def _sotw(**over):
    body = {"metric": {"key": "mining"},
            "ranking": {"mode": "gained"},
            "bonus_rules": []}
    body.update(over)
    return etv.validated_competition_config(None, "sotw", body)


def _botw(**over):
    body = {"npcs": ["Zulrah"],
            "ranking": {"mode": "gained"},
            "bonus_rules": []}
    body.update(over)
    return etv.validated_competition_config(None, "botw", body)


class TestMetric:
    def test_sotw_happy_path(self):
        cfg = _sotw()
        assert cfg["kind"] == "competition" and cfg["metric_kind"] == "skill"
        assert cfg["skill"] == "mining"
        assert "npcs" not in cfg

    def test_sotw_rejects_missing_unknown_and_overall(self):
        with pytest.raises(ProblemException):
            _sotw(metric={})
        with pytest.raises(ProblemException):
            _sotw(metric={"key": "sailing_made_up"})
        with pytest.raises(ProblemException):
            _sotw(metric={"key": "overall"})

    def test_botw_canonicalizes_and_dedupes(self):
        cfg = _botw(npcs=["zulrah", "ZULRAH", "Vorkath"])
        assert cfg["metric_kind"] == "boss"
        assert cfg["npcs"] == ["Zulrah", "Vorkath"]

    def test_botw_rejects_unknown_and_empty(self):
        with pytest.raises(ProblemException):
            _botw(npcs=["Madeupboss"])
        with pytest.raises(ProblemException):
            _botw(npcs=[])

    def test_botw_metric_display_fallback(self):
        cfg = etv.validated_competition_config(
            None, "botw", {"metric": {"display": "Zulrah"}})
        assert cfg["npcs"] == ["Zulrah"]


class TestRanking:
    def test_gained_mode_stores_no_rate(self):
        assert _botw()["ranking"] == {"mode": "gained"}

    def test_points_mode_defaults_by_metric_kind(self):
        assert _botw(ranking={"mode": "points"})["ranking"] == {
            "mode": "points", "gained_per_point": 1}
        assert _sotw(ranking={"mode": "points"})["ranking"] == {
            "mode": "points", "gained_per_point": 10_000}

    def test_points_rate_clamped(self):
        cfg = _botw(ranking={"mode": "points", "gained_per_point": 0})
        assert cfg["ranking"]["gained_per_point"] == etv.COMP_MIN_GAINED_PER_POINT

    def test_bad_mode_rejected(self):
        with pytest.raises(ProblemException):
            _botw(ranking={"mode": "vibes"})


class TestBonusRules:
    def test_time_rule_normalized_with_sequential_ids(self):
        cfg = _botw(bonus_rules=[
            {"type": "time_under", "npc": "zulrah", "threshold_ms": 60_000,
             "points": 5, "max_awards": 3},
            {"type": "time_under", "npc": "Zulrah", "threshold_ms": 50_400,
             "points": 15},
        ])
        rules = cfg["bonus_rules"]
        assert [r["id"] for r in rules] == [1, 2]
        assert rules[0]["npc"] == "Zulrah" and rules[0]["max_awards"] == 3
        assert rules[1]["max_awards"] == 1  # per-player default

    def test_time_rule_npc_implied_for_single_boss_race(self):
        cfg = _botw(bonus_rules=[
            {"type": "time_under", "threshold_ms": 60_000, "points": 5}])
        assert cfg["bonus_rules"][0]["npc"] == "Zulrah"

    def test_time_rule_must_name_a_raced_boss(self):
        with pytest.raises(ProblemException):
            _botw(bonus_rules=[{"type": "time_under", "npc": "Vorkath",
                                "threshold_ms": 60_000, "points": 5}])

    def test_time_rule_threshold_bounds(self):
        for bad in (0, 599, 7 * 60 * 60 * 1000, None, "fast"):
            with pytest.raises(ProblemException):
                _botw(bonus_rules=[{"type": "time_under", "npc": "Zulrah",
                                    "threshold_ms": bad, "points": 5}])

    def test_time_rule_rejected_on_sotw(self):
        with pytest.raises(ProblemException):
            _sotw(bonus_rules=[{"type": "time_under", "npc": "Zulrah",
                                "threshold_ms": 60_000, "points": 5}])

    def test_pet_rule_canonicalizes_names(self):
        cfg = _botw(bonus_rules=[
            {"type": "pet", "points": 100, "pets": ["pet snakeling"]}])
        assert cfg["bonus_rules"][0]["pets"] == ["Pet snakeling"]

    def test_pet_rule_rejects_unknown_pet(self):
        with pytest.raises(ProblemException):
            _botw(bonus_rules=[{"type": "pet", "points": 100,
                                "pets": ["Not a pet"]}])

    def test_sotw_pet_rule_autofills_skilling_pet(self):
        cfg = _sotw(bonus_rules=[{"type": "pet", "points": 50}])
        assert cfg["bonus_rules"][0]["pets"] == ["Rock golem"]

    def test_sotw_pet_rule_without_skilling_pet_rejected(self):
        # Attack has no skilling pet and no explicit list was given.
        with pytest.raises(ProblemException):
            etv.validated_competition_config(
                None, "sotw",
                {"metric": {"key": "attack"},
                 "bonus_rules": [{"type": "pet", "points": 50}]})

    def test_only_one_pet_rule(self):
        with pytest.raises(ProblemException):
            _botw(bonus_rules=[
                {"type": "pet", "points": 100, "pets": ["Pet snakeling"]},
                {"type": "pet", "points": 50, "pets": ["Pet snakeling"]},
            ])

    def test_unknown_rule_type_rejected(self):
        with pytest.raises(ProblemException):
            _botw(bonus_rules=[{"type": "mystery", "points": 5}])

    def test_rule_cap(self):
        rules = [{"type": "time_under", "npc": "Zulrah",
                  "threshold_ms": 60_000 + i, "points": 5}
                 for i in range(etv.COMP_MAX_BONUS_RULES + 1)]
        with pytest.raises(ProblemException):
            _botw(bonus_rules=rules)


class TestBoundsMirror:
    def test_bounds_agree_with_services_competition(self):
        # The validator duplicates the pure module's bounds (no service
        # imports here) — this is the tripwire that keeps the copies honest.
        import importlib.util
        import os

        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        spec = importlib.util.spec_from_file_location(
            "_comp_bounds", os.path.join(root, "services", "competition.py"))
        comp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(comp)
        assert etv.COMP_MAX_NPCS == comp.MAX_NPCS
        assert etv.COMP_MAX_BONUS_RULES == comp.MAX_BONUS_RULES
        assert etv.COMP_MIN_BONUS_POINTS == comp.MIN_BONUS_POINTS
        assert etv.COMP_MAX_BONUS_POINTS == comp.MAX_BONUS_POINTS
        assert etv.COMP_MAX_AWARDS_PER_PLAYER == comp.MAX_AWARDS_PER_PLAYER
        assert etv.COMP_MIN_GAINED_PER_POINT == comp.MIN_GAINED_PER_POINT
        assert etv.COMP_MAX_GAINED_PER_POINT == comp.MAX_GAINED_PER_POINT
        assert etv.COMP_MIN_TIME_THRESHOLD_MS == comp.MIN_TIME_THRESHOLD_MS
        assert etv.COMP_MAX_TIME_THRESHOLD_MS == comp.MAX_TIME_THRESHOLD_MS
        assert etv.COMP_RANKING_MODES == comp.RANKING_MODES


class TestManagedTaskGuard:
    def test_task_routes_refuse_competition_type(self):
        with pytest.raises(ProblemException):
            etv.validate_task_payload(None, {"type": "competition",
                                             "label": "Race"})
