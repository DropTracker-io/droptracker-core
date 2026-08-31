"""Hiscores-rank crossing/seeding decisions (services/rank_milestones.py).

Rank improves DOWNWARD, which inverts every intuition the KC tests encode —
so the cases that matter here are the jitter re-fire (rank oscillating around
a threshold), the added-threshold flood (a group tightening its list must not
re-announce members already past the new threshold), and silent seeding for
first-sighted members.
"""
import pytest

from services.rank_milestones import (
    crossed_threshold,
    evaluate_member_snapshot,
    metric_kind,
    metric_label,
    parse_thresholds,
)


class TestParseThresholds:
    def test_csv(self):
        assert parse_thresholds("10000,5000,1000") == [1000, 5000, 10000]

    def test_json_list_string(self):
        assert parse_thresholds("[10000, 500]") == [500, 10000]

    def test_empty_and_garbage_fall_back_to_defaults(self):
        assert parse_thresholds("") == [1000, 5000, 10000]
        assert parse_thresholds(None) == [1000, 5000, 10000]
        assert parse_thresholds("soon,zero") == [1000, 5000, 10000]

    def test_negatives_and_zero_dropped(self):
        assert parse_thresholds("0,-5,250") == [250]


class TestCrossedThreshold:
    THRESHOLDS = [1000, 5000, 10000]

    def test_entering_a_bracket(self):
        assert crossed_threshold(12000, 9000, self.THRESHOLDS) == 10000

    def test_deepest_threshold_only(self):
        # 20,000 -> 800 is one "top 1,000" message, not three.
        assert crossed_threshold(20000, 800, self.THRESHOLDS) == 1000

    def test_landing_exactly_on_threshold_counts(self):
        assert crossed_threshold(1500, 1000, self.THRESHOLDS) == 1000

    def test_no_stored_value_seeds_silently(self):
        # First sight of a member at rank 800 is not "entered the top 1,000".
        assert crossed_threshold(None, 800, self.THRESHOLDS) is None

    def test_jitter_around_threshold_does_not_refire(self):
        # Best-rank watermark: 9,950 was already inside 10,000, so a bounce to
        # 10,050 and back to 9,900 crosses nothing (prev_best stays 9,950).
        assert crossed_threshold(9950, 9900, self.THRESHOLDS) is None

    def test_added_threshold_does_not_flood(self):
        # A group later adding 7,500: members whose best is already 6,000
        # never see prev_best > 7500, so nothing fires.
        assert crossed_threshold(6000, 5990, [1000, 5000, 7500, 10000]) is None

    def test_worsening_rank_never_fires(self):
        assert crossed_threshold(900, 1100, self.THRESHOLDS) is None

    def test_unranked_never_fires(self):
        assert crossed_threshold(12000, 0, self.THRESHOLDS) is None


class TestMetricClassification:
    def test_kinds(self):
        assert metric_kind("bosses", "zulrah") == "boss"
        assert metric_kind("skills", "attack") == "skill"
        assert metric_kind("activities", "clue_scrolls_hard") == "clue"

    def test_non_clue_activities_out_of_scope(self):
        assert metric_kind("activities", "league_points") is None
        assert metric_kind("computed", "ehb") is None

    def test_labels(self):
        assert metric_label("boss", "chambers_of_xeric") == "Chambers Of Xeric"
        assert metric_label("skill", "runecrafting") == "Runecrafting"
        assert metric_label("clue", "clue_scrolls_hard") == "Hard Clue Scrolls"
        assert metric_label("clue", "clue_scrolls_all") == "Clue Scrolls (all)"


def snapshot(**sections):
    """A minimal WOM bulk-hiscores snapshot data dict."""
    return {
        "bosses": sections.get("bosses", {}),
        "skills": sections.get("skills", {}),
        "activities": sections.get("activities", {}),
    }


class TestEvaluateMemberSnapshot:
    THRESHOLDS = [1000, 5000, 10000]
    ALL_KINDS = ("boss", "skill", "clue")

    def test_first_sight_seeds_everything_silently(self):
        updates, crossings = evaluate_member_snapshot(
            snapshot(bosses={"zulrah": {"kills": 1204, "rank": 800}}),
            stored={},
            thresholds=self.THRESHOLDS,
            enabled_kinds=self.ALL_KINDS,
        )
        assert updates == {"zulrah": 800}
        assert crossings == []

    def test_crossing_fires_with_value_and_label(self):
        updates, crossings = evaluate_member_snapshot(
            snapshot(bosses={"zulrah": {"kills": 1500, "rank": 4200}}),
            stored={"zulrah": 6000},
            thresholds=self.THRESHOLDS,
            enabled_kinds=self.ALL_KINDS,
        )
        assert updates == {"zulrah": 4200}
        assert len(crossings) == 1
        c = crossings[0]
        assert c["threshold"] == 5000
        assert c["rank"] == 4200
        assert c["previous_best_rank"] == 6000
        assert c["metric_kind"] == "boss"
        assert c["value"] == 1500
        assert c["metric_label"] == "Zulrah"

    def test_unranked_skipped_and_never_stored(self):
        updates, crossings = evaluate_member_snapshot(
            snapshot(bosses={"zulrah": {"kills": 3, "rank": 0}}),
            stored={},
            thresholds=self.THRESHOLDS,
            enabled_kinds=self.ALL_KINDS,
        )
        assert updates == {}
        assert crossings == []

    def test_worse_rank_not_stored(self):
        # The watermark is monotone: a worse rank must not overwrite it, or
        # jitter around a threshold would re-fire on the way back in.
        updates, crossings = evaluate_member_snapshot(
            snapshot(bosses={"zulrah": {"kills": 1500, "rank": 10050}}),
            stored={"zulrah": 9950},
            thresholds=self.THRESHOLDS,
            enabled_kinds=self.ALL_KINDS,
        )
        assert updates == {}
        assert crossings == []

    def test_scope_toggles(self):
        snap = snapshot(
            bosses={"zulrah": {"kills": 1500, "rank": 900}},
            skills={"attack": {"experience": 200_000_000, "rank": 900}},
            activities={"clue_scrolls_all": {"score": 2500, "rank": 900}},
        )
        updates, _ = evaluate_member_snapshot(
            snap, stored={}, thresholds=self.THRESHOLDS, enabled_kinds=("skill",)
        )
        assert updates == {"attack": 900}

    def test_clue_classification_by_prefix(self):
        snap = snapshot(activities={
            "clue_scrolls_hard": {"score": 300, "rank": 4000},
            "league_points": {"score": 50000, "rank": 40},
        })
        updates, crossings = evaluate_member_snapshot(
            snap, stored={"clue_scrolls_hard": 6000}, thresholds=self.THRESHOLDS,
            enabled_kinds=self.ALL_KINDS,
        )
        assert updates == {"clue_scrolls_hard": 4000}
        assert len(crossings) == 1
        assert crossings[0]["metric_kind"] == "clue"
        assert crossings[0]["value"] == 300
