"""Unit tests for the bulk library-copy request parser
(``web_api.routes.events.parse_library_bulk_body``).

Stocking a board-game event means filling four difficulty pools, so the bulk
endpoint takes both explicit picks and "give me N of tier X" counts. The
selection rules (de-duplication, the COMBINED cap across both modes, the tier
vocabulary) are where an off-by-one lets a caller create hundreds of tasks in
one request, so they are pinned here rather than left to the endpoint.
"""

import pytest

from web_api.common import ProblemException
from web_api.routes import events as events_routes
from web_api.routes.events import _MAX_BULK_LIBRARY_TASKS, parse_library_bulk_body


@pytest.fixture(autouse=True)
def _real_vocabularies(monkeypatch):
    """conftest stubs ``db`` as a MagicMock, so the module-level
    ``EVENT_TASK_DIFFICULTIES`` / ``EVENT_TASK_TYPES`` imported from
    ``db.models`` are MagicMocks whose ``in`` check is silently False — every
    valid value would look invalid. Pin the real tuples for these tests."""
    monkeypatch.setattr(events_routes, "EVENT_TASK_DIFFICULTIES",
                        ("air", "water", "earth", "fire"))
    monkeypatch.setattr(events_routes, "EVENT_TASK_TYPES",
                        ("item_collection", "pet_collection", "kc_target", "custom"))


class TestExplicitIds:
    def test_dedupes_and_preserves_order(self):
        ids, picks, _ = parse_library_bulk_body({"library_item_ids": [3, 1, 3, 2, 1]})
        assert ids == [3, 1, 2]
        assert picks == []

    def test_rejects_non_positive_ids(self):
        for bad in ([0], [-1], ["7"], [True], [None]):
            with pytest.raises(ProblemException):
                parse_library_bulk_body({"library_item_ids": bad})

    def test_rejects_an_over_long_list(self):
        with pytest.raises(ProblemException):
            parse_library_bulk_body(
                {"library_item_ids": list(range(1, _MAX_BULK_LIBRARY_TASKS + 2))})


class TestTierPicks:
    def test_parses_tier_counts(self):
        _ids, picks, _ = parse_library_bulk_body({"picks": [
            {"difficulty": "air", "count": 10},
            {"difficulty": "fire", "count": 4},
        ]})
        assert picks == [("air", 10), ("fire", 4)]

    def test_zero_count_picks_are_dropped_not_errors(self):
        """The UI sends a row per tier; the untouched ones come through as 0."""
        _ids, picks, _ = parse_library_bulk_body({"picks": [
            {"difficulty": "air", "count": 0},
            {"difficulty": "water", "count": 3},
        ]})
        assert picks == [("water", 3)]

    def test_null_difficulty_is_the_untiered_pool(self):
        _ids, picks, _ = parse_library_bulk_body(
            {"picks": [{"difficulty": None, "count": 5}]})
        assert picks == [(None, 5)]

    def test_rejects_an_unknown_tier(self):
        """The stored values are the legacy rune elements, not the labels the
        UI shows ("easy"/"medium"/...) — sending a label must 422, not
        silently select nothing."""
        with pytest.raises(ProblemException):
            parse_library_bulk_body({"picks": [{"difficulty": "easy", "count": 1}]})

    def test_rejects_a_bad_count(self):
        for bad in ("3", -1, True, None, _MAX_BULK_LIBRARY_TASKS + 1):
            with pytest.raises(ProblemException):
                parse_library_bulk_body(
                    {"picks": [{"difficulty": "air", "count": bad}]})


class TestCombinedLimits:
    def test_cap_spans_both_selection_modes(self):
        half = _MAX_BULK_LIBRARY_TASKS // 2
        # Each half alone is fine…
        parse_library_bulk_body({"library_item_ids": list(range(1, half + 1))})
        parse_library_bulk_body({"picks": [{"difficulty": "air", "count": half}]})
        # …together they must not slip past the cap.
        with pytest.raises(ProblemException):
            parse_library_bulk_body({
                "library_item_ids": list(range(1, half + 2)),
                "picks": [{"difficulty": "air", "count": half}],
            })

    def test_empty_request_is_rejected(self):
        with pytest.raises(ProblemException):
            parse_library_bulk_body({})
        with pytest.raises(ProblemException):
            parse_library_bulk_body({"library_item_ids": [], "picks": []})
        with pytest.raises(ProblemException):
            parse_library_bulk_body({"picks": [{"difficulty": "air", "count": 0}]})


class TestTypeFilter:
    def test_passes_a_valid_type_through(self):
        _ids, _picks, ttype = parse_library_bulk_body(
            {"picks": [{"difficulty": "air", "count": 1}], "type": "item_collection"})
        assert ttype == "item_collection"

    def test_rejects_an_unknown_type(self):
        with pytest.raises(ProblemException):
            parse_library_bulk_body(
                {"library_item_ids": [1], "type": "not_a_task_type"})
