"""Unit tests for services/loot_sweep.py — the pure scoring for the
``loot_sweep`` event kind: decaying per-receipt item points, per-item caps, and
boss-"set" completion bonuses.

Loaded straight from the file (like test_event_engine_scoring) so the conftest
``services`` sys.modules stub never shadows the real module.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "loot_sweep.py",
)
_spec = importlib.util.spec_from_file_location("_loot_sweep_ut", _PATH)
ls = importlib.util.module_from_spec(_spec)
sys.modules["_loot_sweep_ut"] = ls
_spec.loader.exec_module(ls)


def _row(item, qty=1, source_type="drop"):
    """A minimal EventCompletion stand-in for the ledger helpers."""
    return SimpleNamespace(matched_target=item, quantity=qty, source_type=source_type)


# --- receipt decay --------------------------------------------------------- #
class TestReceiptFactor:
    def test_linear_default_matches_grid(self):
        # The authoring grid columns: 100 / 80 / 60 / 40 / 20 %.
        got = [ls.receipt_factor(k, 20, "linear") for k in range(1, 6)]
        assert got == pytest.approx([1.0, 0.8, 0.6, 0.4, 0.2])

    def test_linear_floors_at_zero(self):
        assert ls.receipt_factor(6, 20, "linear") == 0.0
        assert ls.receipt_factor(99, 20, "linear") == 0.0

    def test_geometric(self):
        got = [ls.receipt_factor(k, 20, "geometric") for k in range(1, 4)]
        assert got == pytest.approx([1.0, 0.8, 0.64])

    def test_pre_first_receipt_is_zero(self):
        assert ls.receipt_factor(0, 20) == 0.0


# --- item point totals ----------------------------------------------------- #
class TestItemPoints:
    def test_full_linear_sweep(self):
        # base 40: 40 + 32 + 24 + 16 + 8 = 120.
        assert ls.item_points(40, 5, 5, 20, "linear") == 120

    def test_rounds_each_receipt(self):
        # base 9: round(9) + round(7.2) + round(5.4) = 9 + 7 + 5 = 21.
        assert ls.item_points(9, 3, 5, 20, "linear") == 21

    def test_cap_truncates_receipts(self):
        assert ls.item_points(40, 10, 5, 20, "linear") == 120  # count clamped to max 5
        assert ls.item_points(40, 2, 2, 20, "linear") == 72     # 40 + 32

    def test_zero_receipts(self):
        assert ls.item_points(40, 0, 5, 20, "linear") == 0


# --- config parsing -------------------------------------------------------- #
class TestConfig:
    def test_defaults(self):
        cfg = ls.LootSweepConfig({"items": [{"item_name": "Dragon claws", "points": 10}]})
        assert cfg.decay_percent == 20
        assert cfg.decay_mode == "linear"
        assert cfg.default_max_awards == 5
        assert cfg.set_bonus_points == 0
        assert cfg.set_bonus_max == 1
        assert cfg.items[0]["max_awards"] == 5
        assert cfg.items[0]["counts_for_set"] is True

    def test_per_item_overrides(self):
        cfg = ls.LootSweepConfig({
            "default_max_awards": 3,
            "items": [
                {"item_name": "Pet", "points": 60, "counts_for_set": False},
                {"item_name": "Hilt", "points": 13, "max_awards": 2},
            ],
        })
        assert cfg.by_key["pet"]["counts_for_set"] is False
        assert cfg.by_key["pet"]["max_awards"] == 3     # inherits default
        assert cfg.by_key["hilt"]["max_awards"] == 2     # override
        assert cfg.set_item_keys == ["hilt"]             # pet excluded from set

    def test_invalid_decay_mode_falls_back(self):
        cfg = ls.LootSweepConfig({"decay_mode": "bogus", "items": []})
        assert cfg.decay_mode == "linear"

    def test_accepts_json_string(self):
        # The board endpoint hands EventTask.config (a JSON string) straight in.
        import json as _json
        cfg = ls.LootSweepConfig(_json.dumps(
            {"decay_percent": 25, "items": [{"item_name": "Claws", "points": 10}]}))
        assert cfg.decay_percent == 25
        assert cfg.by_key["claws"]["points"] == 10.0

    def test_garbage_string_is_empty(self):
        cfg = ls.LootSweepConfig("not json{")
        assert cfg.items == [] and cfg.decay_percent == 20

    def test_duplicate_items_deduped(self):
        cfg = ls.LootSweepConfig({"items": [
            {"item_name": "Claws", "points": 5},
            {"item_name": "claws", "points": 99},
        ]})
        assert len(cfg.items) == 1
        assert cfg.items[0]["points"] == 5  # first wins


# --- team scoring ---------------------------------------------------------- #
def _armadyl_config(set_max=1):
    return ls.LootSweepConfig({
        "decay_percent": 20,
        "set_bonus_points": 40,
        "set_bonus_max": set_max,
        "items": [
            {"item_name": "Armadyl helmet", "points": 9},
            {"item_name": "Armadyl chestplate", "points": 9},
            {"item_name": "Armadyl chainskirt", "points": 9},
            {"item_name": "Armadyl hilt", "points": 13},
            {"item_name": "Pet kree'arra", "points": 60, "counts_for_set": False},
        ],
    })


class TestScoreCounts:
    def test_one_of_each_completes_set(self):
        cfg = _armadyl_config()
        counts = {"armadyl helmet": 1, "armadyl chestplate": 1,
                  "armadyl chainskirt": 1, "armadyl hilt": 1}
        r = cfg and ls.score_counts(counts, cfg)
        assert r["item_total"] == 9 + 9 + 9 + 13  # 40
        assert r["sets_completed"] == 1
        assert r["sets_awarded"] == 1
        assert r["set_total"] == 40
        assert r["total"] == 80

    def test_pet_does_not_gate_set(self):
        cfg = _armadyl_config()
        counts = {"armadyl helmet": 1, "armadyl chestplate": 1,
                  "armadyl chainskirt": 1, "armadyl hilt": 1}  # no pet
        assert ls.score_counts(counts, cfg)["sets_completed"] == 1

    def test_pet_scores_but_missing_gear_blocks_set(self):
        cfg = _armadyl_config()
        r = ls.score_counts({"pet kree'arra": 1, "armadyl helmet": 1}, cfg)
        assert r["item_total"] == 60 + 9
        assert r["sets_completed"] == 0
        assert r["set_total"] == 0
        assert r["total"] == 69

    def test_second_set_capped_by_bonus_max(self):
        cfg = _armadyl_config(set_max=1)
        counts = {k: 2 for k in ("armadyl helmet", "armadyl chestplate",
                                 "armadyl chainskirt", "armadyl hilt")}
        r = ls.score_counts(counts, cfg)
        assert r["sets_completed"] == 2
        assert r["sets_awarded"] == 1  # capped
        assert r["set_total"] == 40

    def test_second_set_allowed_when_max_higher(self):
        cfg = _armadyl_config(set_max=3)
        counts = {k: 2 for k in ("armadyl helmet", "armadyl chestplate",
                                 "armadyl chainskirt", "armadyl hilt")}
        r = ls.score_counts(counts, cfg)
        assert r["sets_awarded"] == 2
        assert r["set_total"] == 80

    def test_unknown_ledger_item_ignored(self):
        cfg = _armadyl_config()
        r = ls.score_counts({"twisted bow": 5, "armadyl helmet": 1}, cfg)
        assert r["item_total"] == 9  # bow not in config


class TestScoreRows:
    def test_counts_from_rows_folds_quantity(self):
        rows = [_row("Armadyl helmet"), _row("Armadyl helmet", qty=2)]
        assert ls.counts_from_rows(rows) == {"armadyl helmet": 3}

    def test_bonus_and_nameless_rows_ignored(self):
        rows = [_row("Armadyl helmet"),
                _row(None, qty=5),
                _row("Armadyl hilt", source_type="bonus")]
        assert ls.counts_from_rows(rows) == {"armadyl helmet": 1}

    def test_team_total_end_to_end(self):
        cfg = _armadyl_config()
        rows = [_row("Armadyl helmet"), _row("Armadyl chestplate"),
                _row("Armadyl chainskirt"), _row("Armadyl hilt")]
        assert ls.team_total(rows, cfg) == 80
