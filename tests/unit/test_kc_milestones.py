"""KC milestone crossing/seeding decisions (data/submissions/kc_milestones.py).

The pure half of the feature — what fires and what seeds silently. The trap
this guards against is the one the total-level milestones already hit: a
membership test ("is this KC a milestone?") re-announces on every submission
carrying the same KC, where a crossing test against the stored watermark
announces exactly once.
"""
import pytest

from data.submissions.kc_milestones import (
    KC_SEED_GAP_BOUND,
    highest_crossed_milestone,
    parse_kill_count,
    should_seed_silently,
)


class TestParseKillCount:
    def test_positive_int(self):
        assert parse_kill_count(100) == 100
        assert parse_kill_count("250") == 250

    def test_zero_means_unknown(self):
        # The plugin sends 0 for "unknown", never a real KC of 0.
        assert parse_kill_count(0) is None
        assert parse_kill_count("0") is None

    def test_negative_and_garbage(self):
        assert parse_kill_count(-5) is None
        assert parse_kill_count(None) is None
        assert parse_kill_count("soon") is None
        assert parse_kill_count("") is None


class TestHighestCrossedMilestone:
    def test_simple_crossing(self):
        assert highest_crossed_milestone(99, 100, 100) == 100

    def test_crossing_is_prev_exclusive_new_inclusive(self):
        # prev < m <= new — landing exactly on the milestone fires it once,
        # and the next submission at the same KC does not.
        assert highest_crossed_milestone(100, 100, 100) is None
        assert highest_crossed_milestone(100, 101, 100) is None
        assert highest_crossed_milestone(100, 200, 100) == 200

    def test_multi_cross_announces_only_the_highest(self):
        # 99 -> 400 crosses 100, 200, 300, 400: one message about 400.
        assert highest_crossed_milestone(99, 400, 100) == 400

    def test_zero_interval_disables(self):
        assert highest_crossed_milestone(99, 100, 0) is None

    def test_no_op_on_regression_or_duplicate(self):
        assert highest_crossed_milestone(100, 100, 100) is None
        assert highest_crossed_milestone(200, 100, 100) is None

    def test_interval_from_config_string(self):
        # Group config values arrive as strings.
        assert highest_crossed_milestone(999, 1000, "1000") == 1000

    def test_garbage_interval_disables(self):
        assert highest_crossed_milestone(99, 100, "lots") is None
        assert highest_crossed_milestone(99, 100, None) is None


class TestShouldSeedSilently:
    def test_first_kill_is_not_a_seed(self):
        # (no row, kc=1) is the one announceable cold start.
        assert should_seed_silently(None, 1) is False

    def test_fresh_install_with_history_seeds(self):
        # A first-ever report of 500 KC is stale news, not 5 milestones.
        assert should_seed_silently(None, 500) is True
        assert should_seed_silently(None, 2) is True

    def test_normal_progress_does_not_seed(self):
        assert should_seed_silently(99, 100) is False
        assert should_seed_silently(100, 100 + KC_SEED_GAP_BOUND) is False

    def test_oversized_gap_reseeds(self):
        # Two consecutive reports hundreds of kills apart mean a divergent
        # counter (the Fortis Colosseum trap), a relink, or an outage — all
        # likelier than a legitimate grind between two plugin reports.
        assert should_seed_silently(100, 101 + KC_SEED_GAP_BOUND) is True
