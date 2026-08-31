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

    def test_pet_rules_may_not_claim_the_same_pet_twice(self):
        # Several pet rules are allowed (a headline pet plus a consolation
        # one); overlapping lists are not, or one pet would pay twice.
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
        assert etv.COMP_MAX_RULE_NEED == comp.MAX_RULE_NEED
        assert etv.COMP_MIN_MILESTONE_STEP == comp.MIN_MILESTONE_STEP
        assert etv.COMP_MAX_MILESTONE_STEP == comp.MAX_MILESTONE_STEP
        # The rule-type vocabulary is a bound too, and the one most likely to
        # drift: a type the validator accepts but the scorer doesn't recognise
        # is silently DROPPED from rules_by_id, so its rows pay nothing and its
        # cap falls back to 1. There was no assertion for this pair before.
        assert etv.COMP_BONUS_RULE_TYPES == comp.BONUS_RULE_TYPES
        assert etv.COMP_REPEATABLE_PROGRESS_KINDS == comp.REPEATABLE_PROGRESS_KINDS
        assert etv.COMP_SINGLE_AWARD_TASK_TYPES == comp.SINGLE_AWARD_TASK_TYPES
        assert tuple(etv.COMP_BONUS_TASK_TYPES) == comp.BONUS_TASK_TYPES
        assert set(etv.COMP_SOTW_BONUS_TASK_TYPES) <= set(comp.BONUS_TASK_TYPES)

    def test_progress_kinds_agree_with_the_shared_folds(self):
        from utils.task_progress import PROGRESS_KINDS

        assert etv.COMP_PROGRESS_KINDS == PROGRESS_KINDS
        assert set(etv.COMP_REPEATABLE_PROGRESS_KINDS) <= set(PROGRESS_KINDS)


class TestManagedTaskGuard:
    def test_task_routes_refuse_competition_type(self):
        with pytest.raises(ProblemException):
            etv.validate_task_payload(None, {"type": "competition",
                                             "label": "Race"})


# ── bonus rules that embed a task-builder config (the "any task" vocabulary) ──

KNOWN_ITEMS = {"Tanzanite fang", "Magic fang", "Serpentine visage",
               "Dragon 2h sword"}
_ITEM_BY_NORM = {i.lower(): i for i in KNOWN_ITEMS}


@pytest.fixture
def _stub_items(monkeypatch):
    monkeypatch.setattr(
        etv, "_canonical_item",
        lambda s, name: _ITEM_BY_NORM.get((name or "").strip().lower()),
    )


def _task_rule(task, **over):
    rule = {"type": "task", "points": 25, "task": task}
    rule.update(over)
    return rule


class TestEmbeddedTaskRules:
    def test_single_drop_is_scoped_to_the_raced_boss(self, _stub_items):
        cfg = _botw(bonus_rules=[_task_rule(
            {"type": "item_collection", "target": "Tanzanite fang",
             "target_value": 1})])
        rule = cfg["bonus_rules"][0]
        assert rule["type"] == "task" and rule["points"] == 25
        # The scoping is IN the stored config, where the runtime gate reads it.
        assert rule["task"]["config"]["source_npcs"] == ["Zulrah"]
        assert rule["progress_kind"] == "count" and rule["need"] == 1
        assert rule["kinds"] == ["drop", "clog", "pet"]

    def test_every_item_in_a_set_gets_a_source_entry(self, _stub_items):
        cfg = _botw(bonus_rules=[_task_rule({
            "type": "item_collection",
            "config": {"kind": "all_of",
                       "items": ["Tanzanite fang", "Magic fang",
                                 "Serpentine visage"]},
        })])
        rule = cfg["bonus_rules"][0]
        item_npcs = rule["task"]["config"]["item_npcs"]
        # A MISSING entry fails open (the item would count from anywhere), so
        # "every item is present" is the actual scoping guarantee.
        assert set(item_npcs) == {"Tanzanite fang", "Magic fang",
                                  "Serpentine visage"}
        assert all(v == ["Zulrah"] for v in item_npcs.values())
        assert rule["progress_kind"] == "distinct" and rule["need"] == 3
        # A set saturates, so it can only ever pay once.
        assert rule["max_awards"] == 1

    def test_admin_cannot_widen_the_scope_past_the_race(self, _stub_items):
        cfg = _botw(bonus_rules=[_task_rule({
            "type": "item_collection",
            "config": {"kind": "any_of", "items": ["Dragon 2h sword"],
                       "item_npcs": {"Dragon 2h sword": ["Vorkath"]}},
            "target_value": 1,
        })])
        # The race is Zulrah only; the injected map overwrites wholesale.
        assert (cfg["bonus_rules"][0]["task"]["config"]["item_npcs"]
                == {"Dragon 2h sword": ["Zulrah"]})

    def test_weighted_pool_keeps_its_points_goal(self, _stub_items):
        cfg = _botw(bonus_rules=[_task_rule({
            "type": "item_collection", "target_value": 500,
            "config": {"kind": "point_collection",
                       "items": [{"item_name": "Tanzanite fang", "points": 300},
                                 {"item_name": "Magic fang", "points": 200}]},
        }, max_awards=3)])
        rule = cfg["bonus_rules"][0]
        assert rule["progress_kind"] == "points" and rule["need"] == 500
        # Weighted pools accumulate, so repeats are allowed.
        assert rule["max_awards"] == 3

    def test_loot_value_is_scoped(self, _stub_items):
        cfg = _botw(bonus_rules=[_task_rule(
            {"type": "loot_value", "target_value": 5_000_000})])
        rule = cfg["bonus_rules"][0]
        assert rule["task"]["config"]["source_npcs"] == ["Zulrah"]
        assert rule["progress_kind"] == "count" and rule["need"] == 5_000_000

    def test_fast_kill_task_must_name_a_raced_boss(self, _stub_items):
        with pytest.raises(ProblemException):
            _botw(bonus_rules=[_task_rule(
                {"type": "pb_target", "target": "Vorkath",
                 "target_value": 120})])

    def test_kc_and_xp_are_refused_with_a_pointer_to_milestones(self, _stub_items):
        for bad in ("kc_target", "xp_target"):
            with pytest.raises(ProblemException):
                _botw(bonus_rules=[_task_rule(
                    {"type": bad, "target": "Zulrah", "target_value": 50})])

    def test_sotw_refuses_drop_bonuses_it_cannot_scope(self, _stub_items):
        with pytest.raises(ProblemException):
            _sotw(bonus_rules=[_task_rule(
                {"type": "item_collection", "target": "Tanzanite fang",
                 "target_value": 1})])

    def test_sotw_allows_a_level_goal_pinned_to_the_raced_skill(self):
        cfg = _sotw(bonus_rules=[_task_rule(
            {"type": "skill_target", "target": "fishing", "target_value": 99},
            points=200)])
        rule = cfg["bonus_rules"][0]
        # The admin asked for fishing; the race is mining. The race wins.
        # (Stored in the skill list's display case; the matcher normalizes.)
        assert rule["task"]["target"].lower() == "mining"
        assert rule["need"] == 1 and rule["max_awards"] == 1

    def test_clog_unlocks_count_for_a_scoped_item(self, _stub_items):
        cfg = _botw(bonus_rules=[_task_rule(
            {"type": "item_collection", "target": "Tanzanite fang",
             "target_value": 1})])
        # Without this a source restriction makes a clog NEVER credit, and a
        # log slot filled at the raced boss is a real achievement.
        assert cfg["bonus_rules"][0]["task"]["config"]["clog_sources"] is True

    def test_rule_ids_stay_sequential_across_mixed_types(self, _stub_items):
        cfg = _botw(bonus_rules=[
            {"type": "milestone", "step": 100, "points": 10},
            _task_rule({"type": "item_collection", "target": "Magic fang",
                        "target_value": 1}),
            {"type": "time_under", "npc": "Zulrah", "threshold_ms": 60_000,
             "points": 5},
        ])
        assert [r["id"] for r in cfg["bonus_rules"]] == [1, 2, 3]
        assert [r["type"] for r in cfg["bonus_rules"]] == [
            "milestone", "task", "time_under"]


class TestMilestoneRules:
    def test_step_is_required_and_bounded(self):
        cfg = _botw(bonus_rules=[{"type": "milestone", "step": 100,
                                  "points": 10, "max_awards": 20}])
        rule = cfg["bonus_rules"][0]
        assert rule["step"] == 100 and rule["max_awards"] == 20
        for bad in (None, 0, -5, "many", True):
            with pytest.raises(ProblemException):
                _botw(bonus_rules=[{"type": "milestone", "step": bad,
                                    "points": 10}])


class TestPetRuleDefaults:
    def test_botw_pet_rule_fills_from_the_drop_tables(self, monkeypatch):
        # Before this, a boss race's "+ Pet bonus" 422'd: only SKILL races had
        # a default, and the wizard never sends an explicit pet list.
        monkeypatch.setattr(etv, "_competition_pets_for_npcs",
                            lambda s, npcs: ["Pet snakeling"])
        cfg = _botw(bonus_rules=[{"type": "pet", "points": 100}])
        assert cfg["bonus_rules"][0]["pets"] == ["Pet snakeling"]

    def test_botw_pet_rule_explains_itself_when_the_boss_drops_no_pet(
            self, monkeypatch):
        monkeypatch.setattr(etv, "_competition_pets_for_npcs",
                            lambda s, npcs: [])
        with pytest.raises(ProblemException):
            _botw(bonus_rules=[{"type": "pet", "points": 100}])

    def test_sotw_pet_rule_without_a_skilling_pet_says_why(self):
        with pytest.raises(ProblemException):
            etv.validated_competition_config(
                None, "sotw",
                {"metric": {"key": "attack"},
                 "bonus_rules": [{"type": "pet", "points": 50}]})

    def test_two_pet_rules_may_coexist_when_disjoint(self):
        cfg = _botw(bonus_rules=[
            {"type": "pet", "points": 100, "pets": ["Pet snakeling"]},
            {"type": "pet", "points": 20, "pets": ["Vorki"]},
        ])
        assert len(cfg["bonus_rules"]) == 2

    def test_overlapping_pet_rules_are_refused(self):
        with pytest.raises(ProblemException):
            _botw(bonus_rules=[
                {"type": "pet", "points": 100, "pets": ["Pet snakeling"]},
                {"type": "pet", "points": 20, "pets": ["Pet snakeling"]},
            ])

    def test_a_json_string_config_is_accepted(self, _stub_items):
        # The wizard sends EventTaskInput's shape, whose `config` is a JSON
        # STRING; a hand-built payload sends a dict. Both must work.
        import json

        cfg = _botw(bonus_rules=[_task_rule({
            "type": "item_collection", "target_value": 2,
            "config": json.dumps({"kind": "all_of",
                                  "items": ["Tanzanite fang", "Magic fang"]}),
        })])
        rule = cfg["bonus_rules"][0]
        assert set(rule["task"]["config"]["item_npcs"]) == {"Tanzanite fang",
                                                            "Magic fang"}

    def test_an_either_or_metric_branch_is_refused(self, _stub_items):
        # A kc/loot_value path is only matched in match_task_all's post-loop
        # block, which the competition branch returns before — storing one
        # would leave a branch that silently never credits.
        with pytest.raises(ProblemException):
            _botw(bonus_rules=[_task_rule({
                "type": "item_collection", "target_value": 100,
                "config": {"kind": "any_path", "paths": [
                    {"groups": [{"mode": "all_of", "items": ["Tanzanite fang"]}]},
                    {"metric": "kc", "npcs": ["Zulrah"], "need": 5000},
                ]},
            })])

    def test_a_level_goal_can_only_pay_once(self, _stub_items):
        # skill_target matches on EVERY experience envelope once the level is
        # reached, so a cap above 1 would pay again on the next XP drop.
        cfg = _sotw(bonus_rules=[_task_rule(
            {"type": "skill_target", "target": "mining", "target_value": 70},
            points=25, max_awards=5)])
        assert cfg["bonus_rules"][0]["max_awards"] == 1

    def test_sotw_pet_task_falls_back_to_the_skills_own_pet(self, _stub_items):
        # Without this the wizard's "+ Pet task bonus" stored a bare "any pet"
        # rule — every non-misc pet in the game, from anywhere.
        cfg = _sotw(bonus_rules=[_task_rule(
            {"type": "pet_collection", "target_value": 1}, points=50)])
        assert cfg["bonus_rules"][0]["task"]["config"]["pets"] == ["Rock golem"]

    def test_sotw_pet_task_keeps_an_explicit_category_selection(self, _stub_items):
        cfg = _sotw(bonus_rules=[_task_rule({
            "type": "pet_collection", "target_value": 1,
            "config": {"categories": ["skilling"]}}, points=50)])
        # Dropping `categories` silently WIDENED the admin's selection.
        assert cfg["bonus_rules"][0]["task"]["config"] == {"categories": ["skilling"]}

    def test_sotw_pet_task_on_a_petless_skill_says_why(self, _stub_items):
        with pytest.raises(ProblemException):
            etv.validated_competition_config(
                None, "sotw",
                {"metric": {"key": "attack"},
                 "bonus_rules": [_task_rule(
                     {"type": "pet_collection", "target_value": 1})]})
