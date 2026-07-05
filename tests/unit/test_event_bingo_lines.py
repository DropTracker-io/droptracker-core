"""Unit tests for the pure bingo line/blackout math in services/event_engine.py
(Task 20). Loaded by file path like test_event_engine_matcher.py so the
conftest sys.modules stubs never interfere.
"""

import importlib.util
import os
import sys

import pytest

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_bingo_under_test", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_bingo_under_test"] = engine
_spec.loader.exec_module(engine)

SIZES = (3, 4, 5, 6, 7)


class TestLineDefs:
    @pytest.mark.parametrize("size", SIZES)
    def test_line_count_is_2n_plus_2(self, size):
        lines = engine.line_defs(size)
        assert len(lines) == 2 * size + 2

    @pytest.mark.parametrize("size", SIZES)
    def test_every_line_has_size_cells_in_range(self, size):
        for key, cells in engine.line_defs(size).items():
            assert len(cells) == size, key
            assert all(0 <= i < size * size for i in cells), key

    @pytest.mark.parametrize("size", SIZES)
    def test_keys_are_deterministic(self, size):
        keys = set(engine.line_defs(size))
        expected = {f"r{i}" for i in range(size)} | {f"c{i}" for i in range(size)} | {"d0", "d1"}
        assert keys == expected

    def test_rows_and_cols_5(self):
        lines = engine.line_defs(5)
        assert lines["r0"] == frozenset({0, 1, 2, 3, 4})
        assert lines["r3"] == frozenset({15, 16, 17, 18, 19})
        assert lines["c0"] == frozenset({0, 5, 10, 15, 20})
        assert lines["c4"] == frozenset({4, 9, 14, 19, 24})

    def test_diagonals_5(self):
        lines = engine.line_defs(5)
        assert lines["d0"] == frozenset({0, 6, 12, 18, 24})
        assert lines["d1"] == frozenset({4, 8, 12, 16, 20})

    def test_diagonals_3(self):
        lines = engine.line_defs(3)
        assert lines["d0"] == frozenset({0, 4, 8})
        assert lines["d1"] == frozenset({2, 4, 6})

    def test_diagonals_share_center_on_odd_boards_only(self):
        assert engine.line_defs(7)["d0"] & engine.line_defs(7)["d1"] == {24}
        assert engine.line_defs(4)["d0"] & engine.line_defs(4)["d1"] == frozenset()


class TestCompletedLines:
    def test_empty_board(self):
        assert engine.completed_lines(5, set()) == []

    def test_partial_line_does_not_count(self):
        assert engine.completed_lines(3, {0, 1}) == []

    def test_single_row(self):
        assert engine.completed_lines(3, {0, 1, 2}) == ["r0"]

    def test_single_column(self):
        assert engine.completed_lines(4, {1, 5, 9, 13}) == ["c1"]

    def test_main_diagonal(self):
        assert engine.completed_lines(5, {0, 6, 12, 18, 24}) == ["d0"]

    def test_anti_diagonal(self):
        assert engine.completed_lines(5, {4, 8, 12, 16, 20}) == ["d1"]

    def test_row_col_intersection_completes_both(self):
        # Row 0 + column 0 of a 3×3.
        assert engine.completed_lines(3, {0, 1, 2, 3, 6}) == ["c0", "r0"]

    @pytest.mark.parametrize("size", SIZES)
    def test_full_board_completes_every_line(self, size):
        done = set(range(size * size))
        assert engine.completed_lines(size, done) == sorted(engine.line_defs(size))

    @pytest.mark.parametrize("size", SIZES)
    def test_full_board_minus_one_never_blacks_out(self, size):
        # Dropping a corner breaks exactly one row, one column and one diagonal.
        done = set(range(size * size)) - {0}
        keys = engine.completed_lines(size, done)
        assert "r0" not in keys and "c0" not in keys and "d0" not in keys
        assert len(keys) == 2 * size + 2 - 3

    def test_superset_is_stable(self):
        # Adding unrelated cells never removes a completed line (idempotent
        # derivation: the awarded set can be re-derived at any time).
        base = {0, 1, 2}
        assert set(engine.completed_lines(3, base)) <= set(
            engine.completed_lines(3, base | {5, 7}))

    def test_extra_out_of_range_idxs_are_harmless(self):
        assert engine.completed_lines(3, {0, 1, 2, 99}) == ["r0"]


class TestNoteFormats:
    def test_blackout_note_constant(self):
        assert engine.BLACKOUT_NOTE == "blackout"

    def test_ledger_note_shapes(self):
        # evaluate_bingo_bonuses prefixes line keys with "line:", e.g. line:r3.
        keys = engine.completed_lines(5, set(range(25)))
        notes = {f"line:{k}" for k in keys}
        assert "line:r3" in notes and "line:c1" in notes and "line:d0" in notes
        assert all(n.startswith("line:") for n in notes)
