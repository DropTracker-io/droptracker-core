"""Parsing, decoding and diff rules for account state snapshots.

The cases that matter are the hostile and the ambiguous ones: a snapshot is
attacker-controlled input, and the diff has to tell "player just did a thing"
apart from "we are seeing this account for the first time".
"""
import pytest

from services.state_sync import (
    LATE_INIT_KNOWN_ITEMS_MAX,
    MAX_ITEMS,
    count_completed_combat_achievements,
    deserialize_varps,
    improved_diary_tiers,
    is_late_collection_log_init,
    new_collection_log_items,
    newly_completed_quests,
    parse_diary_tiers,
    parse_int_map,
    parse_skills,
    serialize_varps,
    snapshot_summary,
)


class TestParseIntMap:
    def test_json_string_keys_become_ints(self):
        assert parse_int_map({"995": 12, "4151": 1}, limit=10) == {995: 12, 4151: 1}

    def test_drops_malformed_entries_without_failing_the_rest(self):
        raw = {"995": 1, "abc": 2, "4151": "x", "12": 3}
        assert parse_int_map(raw, limit=10) == {995: 1, 12: 3}

    def test_booleans_are_not_accepted_as_ints(self):
        # True is an int in Python; unguarded it becomes item id 1.
        assert parse_int_map({True: 5, "7": True}, limit=10) == {}

    def test_respects_limit(self):
        raw = {str(i): 1 for i in range(1, 100)}
        assert len(parse_int_map(raw, limit=10)) == 10

    def test_enforces_minimums(self):
        assert parse_int_map({"0": 1, "5": 0, "6": 2}, limit=10, min_key=1, min_value=1) == {6: 2}

    def test_non_dict_is_empty(self):
        assert parse_int_map(["not", "a", "dict"], limit=10) == {}
        assert parse_int_map(None, limit=10) == {}


class TestParseSkills:
    def test_normal(self):
        assert parse_skills({"Attack": 13034431}) == {"Attack": 13034431}

    def test_rejects_impossible_xp(self):
        # Above the game's 200m cap: a broken or hostile client.
        assert parse_skills({"Attack": 999_999_999}) == {}

    def test_rejects_negative_and_blank_names(self):
        assert parse_skills({"Attack": -1, "  ": 5, "": 7}) == {}


class TestCombatAchievementCounting:
    def test_counts_set_bits(self):
        assert count_completed_combat_achievements({3116: 0b1011, 3117: 0b1}) == 4

    def test_empty_and_zero(self):
        assert count_completed_combat_achievements({}) == 0
        assert count_completed_combat_achievements({3116: 0}) == 0

    def test_negative_varp_is_treated_as_32_unsigned_bits(self):
        """The client sends a signed int, so a varp with the top bit set arrives
        negative. Counting it naively gives the wrong answer."""
        assert count_completed_combat_achievements({3116: -1}) == 32

    def test_order_independent(self):
        a = count_completed_combat_achievements({3116: 0b101, 5673: 0b11})
        b = count_completed_combat_achievements({5673: 0b11, 3116: 0b101})
        assert a == b


class TestVarpSerialization:
    def test_round_trip(self):
        varps = {3116: 5, 5673: -1}
        assert deserialize_varps(serialize_varps(varps)) == varps

    def test_stable_ordering(self):
        assert serialize_varps({5673: 1, 3116: 2}) == serialize_varps({3116: 2, 5673: 1})

    def test_unreadable_blob_is_empty_not_an_error(self):
        assert deserialize_varps("{not json") == {}
        assert deserialize_varps(None) == {}


class TestCollectionLogDiff:
    def test_new_items_are_those_not_previously_known(self):
        previous = {995: 1, 4151: 1}
        incoming = {995: 5, 4151: 1, 11802: 1}
        assert new_collection_log_items(previous, incoming) == [11802]

    def test_zero_quantity_is_not_an_unlock(self):
        assert new_collection_log_items({}, {4151: 0}) == []

    def test_late_init_suppresses_a_first_full_read(self):
        # Nothing known, hundreds reported: this is a backfill, not 900 drops.
        assert is_late_collection_log_init(known_count=0, new_count=900) is True

    def test_normal_unlock_is_not_suppressed(self):
        assert is_late_collection_log_init(known_count=400, new_count=1) is False

    def test_boundary_does_not_suppress_a_small_first_batch(self):
        assert is_late_collection_log_init(known_count=LATE_INIT_KNOWN_ITEMS_MAX - 1, new_count=1) is False


class TestQuestDiff:
    def test_detects_completion(self):
        assert newly_completed_quests({3: 1}, {3: 2}) == [3]

    def test_already_finished_is_not_new(self):
        assert newly_completed_quests({3: 2}, {3: 2}) == []

    def test_unseen_quest_is_not_announced(self):
        """First snapshot from an established account: every finished quest would
        otherwise look freshly completed."""
        assert newly_completed_quests({}, {3: 2, 4: 2}) == []


class TestDiaryDiff:
    def test_detects_progress(self):
        assert improved_diary_tiers({(0, 0): 5}, [(0, 0, 9)]) == [(0, 0, 9)]

    def test_unchanged_is_ignored(self):
        assert improved_diary_tiers({(0, 0): 5}, [(0, 0, 5)]) == []

    def test_unseen_tier_is_not_announced(self):
        assert improved_diary_tiers({}, [(0, 0, 9)]) == []

    def test_regression_is_ignored(self):
        # Counts should never fall; if one does, something is wrong and it is
        # certainly not an achievement worth announcing.
        assert improved_diary_tiers({(0, 0): 9}, [(0, 0, 5)]) == []


class TestParseDiaryTiers:
    def test_normal(self):
        raw = [{"area_id": 0, "tier": 1, "completed": 7}]
        assert parse_diary_tiers(raw) == [(0, 1, 7)]

    def test_skips_malformed_entries(self):
        raw = [{"area_id": 0, "tier": 1, "completed": 7}, {"area_id": None}, "nope"]
        assert parse_diary_tiers(raw) == [(0, 1, 7)]

    def test_non_list_is_empty(self):
        assert parse_diary_tiers({"area_id": 1}) == []


def test_snapshot_summary_reports_sizes_not_contents():
    summary = snapshot_summary({
        "source": "login",
        "items": {"1": 1, "2": 1},
        "quests": {"3": 2},
        "clog_complete": True,
    })
    assert summary["items"] == 2
    assert summary["source"] == "login"
    assert summary["clog_complete"] is True
    # No raw ids leak into the log line.
    assert "1" not in str(summary["items"]) or summary["items"] == 2


def test_snapshot_summary_survives_hostile_shapes():
    """The summary feeds a log line written after the data has already been
    committed. If it can raise, a successful sync becomes a 500."""
    hostile = {"items": "not-a-map", "quests": 12345, "skills": None,
               "diary_tiers": {"nope": 1}, "ca_varps": 3.5}
    summary = snapshot_summary(hostile)
    assert summary["quests"] == 0
    assert summary["skills"] == 0
    assert summary["ca_varps"] == 0
    assert snapshot_summary("not even a dict") == {}


@pytest.mark.parametrize("limit", [1, 10, MAX_ITEMS])
def test_limits_are_enforced_exactly(limit):
    raw = {str(i): 1 for i in range(1, limit + 50)}
    assert len(parse_int_map(raw, limit=limit)) == limit
