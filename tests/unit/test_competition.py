"""Unit tests for services/competition.py — the pure SOTW/BOTW scoring module
(config parsing, ledger folds with per-player bonus caps, standings merge,
display helpers). Loaded by file path so the conftest ``services`` stub never
interferes.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PATH = os.path.join(_ROOT, "services", "competition.py")
_spec = importlib.util.spec_from_file_location("_competition_under_test", _PATH)
comp = importlib.util.module_from_spec(_spec)
sys.modules["_competition_under_test"] = comp
_spec.loader.exec_module(comp)


SOTW_CFG = {
    "kind": "competition",
    "metric_kind": "skill",
    "skill": "Mining",
    "ranking": {"mode": "points", "gained_per_point": 10_000},
    "bonus_rules": [
        {"id": 1, "type": "pet", "points": 50, "max_awards": 1,
         "pets": ["Rock golem"]},
    ],
}

BOTW_CFG = {
    "kind": "competition",
    "metric_kind": "boss",
    "npcs": ["Zulrah", "zulrah", "Vorkath"],   # dupe folds away
    "ranking": {"mode": "gained"},
    "bonus_rules": [
        {"id": 1, "type": "pet", "points": 100, "max_awards": 1,
         "pets": ["Pet snakeling"]},
        {"id": 2, "type": "time_under", "npc": "Zulrah",
         "threshold_ms": 60_000, "points": 5, "max_awards": 2},
        {"id": 3, "type": "time_under", "npc": "Zulrah",
         "threshold_ms": 50_400, "points": 15, "max_awards": 1},
    ],
}


def _row(player_id, qty, note=None, rid=None, created=None,
         matched_target=None, source_type=None):
    return SimpleNamespace(id=rid, player_id=player_id, quantity=qty,
                           note=note, created_at=created,
                           matched_target=matched_target,
                           source_type=source_type)


# ── config parsing ───────────────────────────────────────────────────────────

class TestConfig:
    def test_sotw_parses(self):
        cfg = comp.CompetitionConfig(SOTW_CFG)
        assert cfg.valid and cfg.metric_kind == "skill"
        assert cfg.skill == "mining"
        assert cfg.ranking_mode == "points" and cfg.gained_per_point == 10_000
        assert len(cfg.bonus_rules) == 1 and cfg.bonus_rules[0].pets == ("rock golem",)

    def test_botw_parses_and_dedupes_npcs(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        assert cfg.valid and cfg.npcs == ("zulrah", "vorkath")
        assert cfg.ranking_mode == "gained"
        # Default rate for a boss race is 1 kill = 1 pt (points mode only).
        assert cfg.gained_per_point == 1
        assert {r.id for r in cfg.bonus_rules} == {1, 2, 3}

    def test_json_string_config_accepted(self):
        import json
        cfg = comp.CompetitionConfig(json.dumps(BOTW_CFG))
        assert cfg.valid and cfg.npcs[0] == "zulrah"

    def test_invalid_configs(self):
        assert not comp.CompetitionConfig(None).valid
        assert not comp.CompetitionConfig({}).valid
        assert not comp.CompetitionConfig({"metric_kind": "skill"}).valid
        assert not comp.CompetitionConfig({"metric_kind": "boss", "npcs": []}).valid

    def test_bad_bonus_rules_dropped(self):
        cfg = comp.CompetitionConfig({
            "metric_kind": "boss", "npcs": ["Zulrah"],
            "bonus_rules": [
                {"type": "pet", "points": 10},                    # no pets
                {"type": "time_under", "points": 5},              # no npc/threshold
                {"type": "nonsense", "points": 5},
                {"type": "time_under", "npc": "Zulrah",
                 "threshold_ms": 300, "points": 5},               # sub-tick
            ],
        })
        assert cfg.bonus_rules == ()

    def test_matcher_index_is_plain_data(self):
        idx = comp.CompetitionConfig(BOTW_CFG).matcher_index()
        assert idx["npcs"] == ["zulrah", "vorkath"]
        assert idx["pet_rules"] == {"pet snakeling": {"id": 1, "points": 100}}
        assert [r["id"] for r in idx["time_rules"]] == [2, 3]


# ── bonus-note tags ──────────────────────────────────────────────────────────

class TestBonusNotes:
    def test_round_trip(self):
        note = comp.bonus_note("time_under", 2)
        assert note == "bonus:time_under:2"
        assert comp.parse_bonus_note(note) == ("time_under", 2)

    def test_tolerates_admin_suffix(self):
        assert comp.parse_bonus_note("bonus:pet:1 | Pet snakeling") == ("pet", 1)
        assert comp.parse_bonus_note("bonus:time_under:3 | 0:52.6") == ("time_under", 3)

    def test_rejects_junk(self):
        for junk in (None, "", "path:2", "bonus:", "bonus:pet", "bonus:pet:x",
                     "bonus::1", "bonus:has spaces:1", "bonus:" + "x" * 33 + ":1"):
            assert comp.parse_bonus_note(junk) is None

    def test_tolerates_a_rule_type_this_deploy_has_never_heard_of(self):
        # A row written by a newer deploy must still READ as a bonus row: an
        # unparsed row is folded into gained, so failing closed here would let
        # a forward-compatible ledger silently inflate the ranked metric.
        assert comp.parse_bonus_note("bonus:future_thing:3") == ("future_thing", 3)
        assert comp.parse_bonus_note("bonus:future_thing:3 | note") == ("future_thing", 3)

    def test_unknown_rule_type_pays_nothing_and_never_becomes_gained(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        per = comp.fold_rows(
            [_row(5, 40),
             _row(5, 999, note="bonus:future_thing:9")], cfg)
        assert per[5]["gained"] == 40
        assert per[5]["bonus_points"] == 0


# ── ledger folds ─────────────────────────────────────────────────────────────

class TestFold:
    def test_gained_and_bonus_split_by_note(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        rows = [
            _row(5, 3, rid=1),                                  # 3 kills
            _row(5, 5, note="bonus:time_under:2 | 0:55", rid=2),
            _row(6, 10, rid=3),
        ]
        per = comp.fold_rows(rows, cfg)
        assert per[5] == {"gained": 3, "bonus_points": 5,
                          "bonus": {2: {"type": "time_under", "count": 1,
                                        "awarded": 1, "points": 5}}}
        assert per[6]["gained"] == 10 and per[6]["bonus_points"] == 0

    def test_per_player_cap_enforced_in_fold(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)  # rule 2: max_awards 2
        rows = [
            _row(5, 5, note="bonus:time_under:2", rid=1),
            _row(5, 5, note="bonus:time_under:2", rid=2),
            _row(5, 5, note="bonus:time_under:2", rid=3),  # over the cap
        ]
        per = comp.fold_rows(rows, cfg)
        slot = per[5]["bonus"][2]
        assert slot["count"] == 3 and slot["awarded"] == 2
        assert per[5]["bonus_points"] == 10  # third row pays nothing

    def test_cap_self_heals_on_revoke(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        rows = [
            _row(5, 5, note="bonus:time_under:2", rid=1),
            _row(5, 5, note="bonus:time_under:2", rid=3),
        ]
        # Rows are what SURVIVED (the revoked one is simply absent).
        per = comp.fold_rows(rows, cfg)
        assert per[5]["bonus"][2]["awarded"] == 2

    def test_award_order_is_deterministic(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        rows = [
            _row(5, 5, note="bonus:time_under:2", rid=9),
            _row(5, 5, note="bonus:time_under:2", rid=2),
        ]
        per = comp.fold_rows(rows, cfg)
        assert per[5]["bonus_points"] == 10

    def test_bonus_award_count(self):
        rows = [
            _row(5, 5, note="bonus:time_under:2", rid=1),
            _row(5, 5, note="bonus:time_under:3", rid=2),
            _row(6, 5, note="bonus:time_under:2", rid=3),
            _row(5, 7, rid=4),
        ]
        assert comp.bonus_award_count(rows, 5, 2) == 1
        assert comp.bonus_award_count(rows, 5, 3) == 1
        assert comp.bonus_award_count(rows, 6, 2) == 1
        assert comp.bonus_award_count(rows, 6, 3) == 0


# ── ranking / totals ─────────────────────────────────────────────────────────

class TestRanking:
    def test_points_conversion_floors(self):
        cfg = comp.CompetitionConfig(SOTW_CFG)
        assert comp.points_for_gained(0, cfg) == 0
        assert comp.points_for_gained(9_999, cfg) == 0
        assert comp.points_for_gained(25_000, cfg) == 2

    def test_player_points_and_rank_value(self):
        cfg = comp.CompetitionConfig(SOTW_CFG)  # points mode
        entry = {"gained": 25_000, "bonus_points": 50}
        assert comp.player_points(entry, cfg) == 52
        assert comp.rank_value(entry, cfg) == 52
        gained_cfg = comp.CompetitionConfig(BOTW_CFG)  # gained mode
        assert comp.rank_value({"gained": 312, "bonus_points": 99}, gained_cfg) == 312

    def test_team_totals(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        per = {5: {"gained": 300, "bonus_points": 5},
               6: {"gained": 100, "bonus_points": 0}}
        gained_total, score_total = comp.team_totals(per, cfg)
        assert gained_total == 400
        assert score_total == 400  # gained mode ranks by gained


# ── standings ────────────────────────────────────────────────────────────────

class TestStandings:
    def test_ranked_by_mode_with_ties_stable(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        per = {
            5: {"gained": 300, "bonus_points": 5, "bonus": {}},
            6: {"gained": 400, "bonus_points": 0, "bonus": {}},
            7: {"gained": 300, "bonus_points": 0, "bonus": {}},
        }
        names = {5: "Alice", 6: "Bob", 7: "Cara"}
        rows = comp.standings(per, cfg, names)
        assert [r["player_name"] for r in rows] == ["Bob", "Alice", "Cara"]
        assert [r["rank"] for r in rows] == [1, 2, 3]

    def test_wom_only_rows_merge_and_dedupe(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        per = {5: {"gained": 300, "bonus_points": 0, "bonus": {}}}
        names = {5: "Alice"}
        wom_rows = [
            {"wom_player_id": 900, "display_name": "Stranger", "gained": 350},
            {"wom_player_id": 901, "display_name": "ALICE", "gained": 280},   # same name
            {"wom_player_id": 902, "display_name": "Resolved", "gained": 100,
             "player_id": 5},                                                  # same player
            "garbage",
        ]
        rows = comp.standings(per, cfg, names, wom_rows)
        assert [r["player_name"] for r in rows] == ["Stranger", "Alice"]
        stranger = rows[0]
        assert stranger["registered"] is False and stranger["bonus_points"] == 0
        assert stranger["wom_player_id"] == 900

    def test_points_mode_counts_bonus(self):
        cfg = comp.CompetitionConfig(SOTW_CFG)
        per = {
            5: {"gained": 100_000, "bonus_points": 0, "bonus": {}},   # 10 pts
            6: {"gained": 50_000, "bonus_points": 50, "bonus": {}},   # 55 pts
        }
        rows = comp.standings(per, cfg, {5: "A", 6: "B"})
        assert rows[0]["player_name"] == "B" and rows[0]["points"] == 55


# ── display helpers ──────────────────────────────────────────────────────────

class TestDisplay:
    def test_format_time_ms(self):
        assert comp.format_time_ms(91_800) == "1:31.8"
        assert comp.format_time_ms(52_000) == "0:52"
        assert comp.format_time_ms(60_000) == "1:00"

    def test_format_gained(self):
        assert comp.format_gained(2_481_034, "skill") == "2.48M XP"
        assert comp.format_gained(312, "boss") == "312 KC"
        assert comp.format_gained(1_500_000_000, "skill") == "1.5B XP"

    def test_score_text(self):
        assert comp.score_text(213, comp.CompetitionConfig(SOTW_CFG)) == "213 pts"
        assert comp.score_text(312, comp.CompetitionConfig(BOTW_CFG)) == "312 KC"

    def test_metric_line(self):
        assert "Mining" in comp.metric_line(comp.CompetitionConfig(SOTW_CFG))
        line = comp.metric_line(comp.CompetitionConfig(BOTW_CFG))
        assert "Zulrah" in line and "kills" in line
        assert comp.metric_line(comp.CompetitionConfig({})) is None

    def test_rule_label_and_bonus_detail(self):
        cfg = comp.CompetitionConfig(BOTW_CFG)
        time_rule = cfg.rules_by_id[2]
        assert "1:00" in comp.rule_label(time_rule)
        detail = comp.bonus_detail(2, cfg, awarded_n=2, time_text="0:55")
        assert detail["points"] == 5 and detail["cap_line"] == "Award 2 of 2"
        assert "0:55" in detail["reason"] and "1:00" in detail["reason"]
        pet_detail = comp.bonus_detail(1, cfg, awarded_n=1,
                                       matched_target="Pet snakeling")
        assert pet_detail["cap_line"] is None
        assert "Pet snakeling" in pet_detail["reason"]
        assert comp.bonus_detail(99, cfg, awarded_n=1)["points"] == 0


# ── embedded task rules + milestones ─────────────────────────────────────────

SET_RULE = {
    "id": 1, "type": "task", "points": 25, "max_awards": 1,
    "progress_kind": "distinct", "need": 3,
    "task": {"type": "item_collection",
             "config": {"kind": "all_of",
                        "items": ["Tanzanite fang", "Magic fang",
                                  "Serpentine visage"]}},
}
# A weighted pool. The MATCHER applies the weight (item_match_quantity returns
# the item's points as the row quantity for a point_collection config), so the
# "points" fold sums those pre-weighted quantities flat — applying the weights a
# second time would square them. test_competition_engine.TestWeightedPoolEndToEnd
# pins the two halves together.
POOL_RULE = {
    "id": 2, "type": "task", "points": 10, "max_awards": 3,
    "progress_kind": "points", "need": 500,
    "task": {"type": "item_collection",
             "config": {"kind": "point_collection",
                        "items": [{"item_name": "Tanzanite fang", "points": 300},
                                  {"item_name": "Magic fang", "points": 200}]}},
}
MILESTONE_RULE = {"id": 3, "type": "milestone", "step": 100, "points": 10,
                  "max_awards": 5}

TASK_CFG = {
    "kind": "competition", "metric_kind": "boss", "npcs": ["Zulrah"],
    "ranking": {"mode": "points", "gained_per_point": 1},
    "bonus_rules": [SET_RULE, POOL_RULE, MILESTONE_RULE],
}


def _task_row(player_id, item, qty=1, rid=1, row_id=None):
    return _row(player_id, qty, note=f"bonus:task:{rid}", rid=row_id,
                matched_target=item)


class TestTaskRuleFold:
    def test_a_partly_collected_set_pays_nothing(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        per = comp.fold_rows([_task_row(1, "Tanzanite fang"),
                              _task_row(1, "Magic fang")], cfg)
        slot = per[1]["bonus"][1]
        assert slot["progress"] == 2 and slot["need"] == 3
        assert slot["awarded"] == 0 and per[1]["bonus_points"] == 0

    def test_completing_the_set_pays_the_rule_not_the_rows(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        per = comp.fold_rows([_task_row(1, "Tanzanite fang"),
                              _task_row(1, "Magic fang"),
                              _task_row(1, "Serpentine visage")], cfg)
        # 25 for the rule — NOT 3 (one per row) and not 75.
        assert per[1]["bonus_points"] == 25
        assert per[1]["bonus"][1]["awarded"] == 1

    def test_a_stack_of_one_listed_item_is_still_one_item(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        per = comp.fold_rows([_task_row(1, "Tanzanite fang", qty=50)], cfg)
        assert per[1]["bonus"][1]["progress"] == 1
        assert per[1]["bonus_points"] == 0

    def test_credit_units_never_leak_into_gained(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        per = comp.fold_rows([_row(1, 40),                      # 40 kills
                              _task_row(1, "Tanzanite fang", qty=300, rid=2)],
                             cfg)
        assert per[1]["gained"] == 40
        assert per[1]["bonus"][2]["progress"] == 300

    def test_weighted_pool_pays_per_need_and_repeats_to_its_cap(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        # 300 + 200 + 300 + 200 = 1000 points of loot = two 500-point awards.
        # Quantities are the weights the matcher already applied.
        rows = [_task_row(1, "Tanzanite fang", qty=300, rid=2, row_id=i)
                if i % 2 == 0 else _task_row(1, "Magic fang", qty=200, rid=2,
                                             row_id=i)
                for i in range(4)]
        per = comp.fold_rows(rows, cfg)
        slot = per[1]["bonus"][2]
        assert slot["progress"] == 1000 and slot["awarded"] == 2
        assert slot["points"] == 20

    def test_revoking_a_row_takes_the_award_back(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        full = [_task_row(1, "Tanzanite fang"), _task_row(1, "Magic fang"),
                _task_row(1, "Serpentine visage")]
        assert comp.fold_rows(full, cfg)[1]["bonus_points"] == 25
        assert comp.fold_rows(full[:-1], cfg)[1]["bonus_points"] == 0

    def test_a_deleted_rule_pays_nothing_rather_than_becoming_gained(self):
        cfg = comp.CompetitionConfig(
            {**TASK_CFG, "bonus_rules": [MILESTONE_RULE]})
        per = comp.fold_rows([_row(1, 50),
                              _task_row(1, "Tanzanite fang", qty=300)], cfg)
        assert per[1]["gained"] == 50 and per[1]["bonus"][1]["points"] == 0


class TestMilestoneFold:
    def test_pays_per_step_with_no_ledger_rows_of_its_own(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        per = comp.fold_rows([_row(1, 250)], cfg)
        slot = per[1]["bonus"][3]
        assert slot["awarded"] == 2 and slot["points"] == 20
        assert per[1]["bonus_points"] == 20

    def test_caps_at_max_awards(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        per = comp.fold_rows([_row(1, 100_000)], cfg)
        assert per[1]["bonus"][3]["awarded"] == 5
        assert per[1]["bonus_points"] == 50

    def test_below_one_step_pays_nothing_and_adds_no_slot(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        per = comp.fold_rows([_row(1, 99)], cfg)
        assert per[1]["bonus_points"] == 0 and 3 not in per[1]["bonus"]

    def test_counts_toward_the_ranked_total_in_points_mode(self):
        cfg = comp.CompetitionConfig(TASK_CFG)
        per = comp.fold_rows([_row(1, 250)], cfg)
        # 250 kills at 1/pt + two 10-point milestones.
        assert comp.rank_value(per[1], cfg) == 270


class TestTaskRuleShape:
    def test_a_saturating_progress_kind_can_only_pay_once(self):
        cfg = comp.CompetitionConfig(
            {**TASK_CFG,
             "bonus_rules": [{**SET_RULE, "max_awards": 5}]})
        assert cfg.rules_by_id[1].max_awards == 1

    def test_matcher_index_carries_the_embedded_task_verbatim(self):
        index = comp.CompetitionConfig(TASK_CFG).matcher_index()
        assert [r["id"] for r in index["task_rules"]] == [1, 2]
        assert index["task_rules"][0]["task"]["type"] == "item_collection"

    def test_a_rule_with_no_embedded_task_is_dropped(self):
        cfg = comp.CompetitionConfig(
            {**TASK_CFG, "bonus_rules": [{"id": 1, "type": "task",
                                          "points": 25}]})
        assert cfg.bonus_rules == ()


class TestRuleWording:
    def test_a_milestone_names_the_races_unit_not_the_steps_size(self):
        # A 1,000-KILL milestone on a boss race used to read "Every 1,000 XP"
        # because the unit was guessed from the step's magnitude.
        boss = comp.CompetitionConfig(
            {**BOTW_CFG, "bonus_rules": [{"id": 1, "type": "milestone",
                                          "step": 1000, "points": 10}]})
        assert comp.rule_label(boss.bonus_rules[0]) == "Every 1,000 kills"
        skill = comp.CompetitionConfig(
            {**SOTW_CFG, "bonus_rules": [{"id": 1, "type": "milestone",
                                          "step": 500, "points": 10}]})
        assert comp.rule_label(skill.bonus_rules[0]) == "Every 500 XP"

    def test_a_level_goal_rule_is_pinned_to_one_award_even_if_stored_otherwise(self):
        # Enforced in the SCORER as well as the validator, so a config written
        # before the pin is corrected on read.
        cfg = comp.CompetitionConfig({**SOTW_CFG, "bonus_rules": [{
            "id": 1, "type": "task", "points": 25, "max_awards": 9,
            "progress_kind": "count", "need": 1,
            "task": {"type": "skill_target", "target": "mining",
                     "target_value": 70}}]})
        assert cfg.rules_by_id[1].max_awards == 1
