"""
Unit tests for data/submissions/drop.py — drop-specific helper functions.

The _normalize_incoming_players function handles all the different formats
the RuneLite plugin can send participant lists (for split GP tracking).
"""

import pytest


@pytest.fixture(autouse=True)
def _import_helpers(request):
    from data.submissions.drop import _normalize_incoming_players
    request.cls._normalize = staticmethod(_normalize_incoming_players) if request.cls else None


class TestNormalizeIncomingPlayers:
    @pytest.fixture(autouse=True)
    def _bind(self):
        from data.submissions.drop import _normalize_incoming_players
        self.normalize = _normalize_incoming_players

    # ── None / empty inputs ──────────────────────────────────────────────────

    def test_none_input_returns_none(self):
        assert self.normalize(None) is None

    def test_empty_list_returns_none(self):
        assert self.normalize([]) is None

    def test_empty_string_returns_none(self):
        assert self.normalize("") is None

    def test_whitespace_only_string_returns_none(self):
        assert self.normalize("   ") is None

    def test_list_of_empty_strings_returns_none(self):
        assert self.normalize(["", "  ", ""]) is None

    # ── List inputs ──────────────────────────────────────────────────────────

    def test_simple_list_passthrough(self):
        result = self.normalize(["Alice", "Bob"])
        assert result == ["Alice", "Bob"]

    def test_list_strips_whitespace(self):
        result = self.normalize(["  Alice  ", "Bob  "])
        assert result == ["Alice", "Bob"]

    def test_list_filters_empty_entries(self):
        result = self.normalize(["Alice", "", "Bob", "  "])
        assert result == ["Alice", "Bob"]

    def test_single_item_list(self):
        result = self.normalize(["Zezima"])
        assert result == ["Zezima"]

    # ── JSON string inputs ───────────────────────────────────────────────────

    def test_json_array_string(self):
        result = self.normalize('["Alice", "Bob"]')
        assert result == ["Alice", "Bob"]

    def test_json_array_string_strips_whitespace(self):
        result = self.normalize('["  Alice  ", "Bob"]')
        assert result == ["Alice", "Bob"]

    def test_json_array_empty_string(self):
        result = self.normalize('[]')
        assert result is None

    def test_invalid_json_falls_back_to_csv(self):
        # Malformed JSON starting with '[' falls back to comma-split
        result = self.normalize("[Alice, Bob")
        assert result == ["[Alice", "Bob"]

    # ── CSV / plain string inputs ─────────────────────────────────────────────

    def test_comma_separated_string(self):
        result = self.normalize("Alice, Bob")
        assert result == ["Alice", "Bob"]

    def test_comma_separated_no_spaces(self):
        result = self.normalize("Alice,Bob,Charlie")
        assert result == ["Alice", "Bob", "Charlie"]

    def test_newline_separated_string(self):
        result = self.normalize("Alice\nBob")
        assert result == ["Alice", "Bob"]

    def test_mixed_newline_and_comma(self):
        result = self.normalize("Alice\nBob,Charlie")
        assert result == ["Alice", "Bob", "Charlie"]

    def test_single_name_string(self):
        result = self.normalize("Zezima")
        assert result == ["Zezima"]

    # ── Dict inputs ──────────────────────────────────────────────────────────

    def test_dict_values_extracted(self):
        result = self.normalize({"0": "Alice", "1": "Bob"})
        assert set(result) == {"Alice", "Bob"}

    def test_empty_dict_returns_none(self):
        result = self.normalize({})
        assert result is None

    # ── Unsupported types ─────────────────────────────────────────────────────

    def test_integer_input_returns_none(self):
        result = self.normalize(42)
        assert result is None

    def test_boolean_input_returns_none(self):
        result = self.normalize(True)
        assert result is None
