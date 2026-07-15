"""services/boardgame_engine.py — pure parts (settings, dice, coins, mercy).

The conftest stubs the ``services`` package, so the real module loads by
file path (the test_event_types.py pattern). DB-touching functions are
covered by integration tests; here we pin the config/dice/coin math the
whole turn loop hangs off."""
from __future__ import annotations

import importlib.util
import random
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

_PATH = Path(__file__).resolve().parent.parent.parent / "services" / "boardgame_engine.py"
_spec = importlib.util.spec_from_file_location("_real_boardgame_engine", _PATH)
bg = importlib.util.module_from_spec(_spec)
sys.modules["_real_boardgame_engine"] = bg
_spec.loader.exec_module(bg)


class TestBoardSettings:
    def test_none_returns_defaults(self):
        s = bg.board_settings(None)
        assert s == bg.DEFAULT_BOARD_SETTINGS
        assert s is not bg.DEFAULT_BOARD_SETTINGS  # deep copy, not the constant

    def test_corrupt_json_returns_defaults(self):
        assert bg.board_settings("{nope") == bg.DEFAULT_BOARD_SETTINGS
        assert bg.board_settings("[1,2]") == bg.DEFAULT_BOARD_SETTINGS

    def test_partial_document_merges_key_by_key(self):
        s = bg.board_settings('{"movement": {"dice_count": 2}, "coins": {"enabled": false}}')
        assert s["movement"]["dice_count"] == 2
        assert s["movement"]["dice_sides"] == 6          # untouched sibling
        assert s["coins"]["enabled"] is False
        assert s["coins"]["per_difficulty"]["fire"] == 50  # untouched nested
        assert s["mercy"]["enabled"] is True               # untouched section

    def test_dict_input_accepted(self):
        s = bg.board_settings({"tile_render": {"mode": "outline", "outline_width": 4}})
        assert s["tile_render"]["mode"] == "outline"
        assert s["tile_render"]["outline_width"] == 4
        assert s["tile_render"]["outline_color"] == "#ffcc33"


class TestRollDice:
    def test_seeded_dice_are_deterministic(self):
        s = bg.board_settings({"movement": {"dice_count": 2, "dice_sides": 6}})
        a = bg.roll_dice(s, random.Random(42))
        b = bg.roll_dice(s, random.Random(42))
        assert a == b and len(a) == 2
        assert all(1 <= f <= 6 for f in a)

    def test_fixed_step_returns_single_pseudo_face(self):
        s = bg.board_settings({"movement": {"mode": "fixed_step", "fixed_step": 3}})
        assert bg.roll_dice(s) == [3]

    def test_fixed_step_floor_is_one(self):
        s = bg.board_settings({"movement": {"mode": "fixed_step", "fixed_step": 0}})
        assert bg.roll_dice(s) == [1]

    def test_dice_bounds_clamped(self):
        s = bg.board_settings({"movement": {"dice_count": 99, "dice_sides": 1}})
        faces = bg.roll_dice(s, random.Random(1))
        assert len(faces) == 8          # count capped
        assert all(1 <= f <= 2 for f in faces)  # sides floored to 2

    def test_garbage_settings_fall_back(self):
        s = bg.board_settings({"movement": {"dice_count": "x", "dice_sides": None}})
        faces = bg.roll_dice(s, random.Random(1))
        assert len(faces) == 1 and 1 <= faces[0] <= 6


class TestCoinReward:
    def test_ladder(self):
        s = bg.board_settings(None)
        assert bg.coin_reward(s, "air") == 10
        assert bg.coin_reward(s, "water") == 20
        assert bg.coin_reward(s, "earth") == 30
        assert bg.coin_reward(s, "fire") == 50

    def test_unknown_or_missing_difficulty_uses_default(self):
        s = bg.board_settings(None)
        assert bg.coin_reward(s, None) == 10
        assert bg.coin_reward(s, "banana") == 10

    def test_disabled_coins_award_zero(self):
        s = bg.board_settings({"coins": {"enabled": False}})
        assert bg.coin_reward(s, "fire") == 0

    def test_override_ladder(self):
        s = bg.board_settings({"coins": {"per_difficulty": {"air": 1}}})
        assert bg.coin_reward(s, "air") == 1

    def test_negative_values_clamped(self):
        s = bg.board_settings({"coins": {"per_difficulty": {"air": -5}, "default": -3}})
        assert bg.coin_reward(s, "air") == 0
        assert bg.coin_reward(s, None) == 0


class TestMercyDeadline:
    def test_grows_with_mercy_count(self):
        s = bg.board_settings(None)
        d0 = bg._mercy_deadline(s, 0)
        d2 = bg._mercy_deadline(s, 2)
        assert d0 and d2
        # base 24h; +12h per prior mercy → exactly 24h apart.
        assert abs((d2 - d0).total_seconds() - 24 * 3600) < 5

    def test_disabled_returns_none(self):
        s = bg.board_settings({"mercy": {"enabled": False}})
        assert bg._mercy_deadline(s, 0) is None


class TestFinishIdx:
    def _tile(self, idx, kind="normal"):
        return SimpleNamespace(idx=idx, tile_kind=kind)

    def test_last_tile_by_default(self):
        tiles = [self._tile(i) for i in range(5)]
        assert bg.finish_idx(tiles) == 4

    def test_explicit_finish_wins(self):
        tiles = [self._tile(0), self._tile(1, "finish"), self._tile(2)]
        assert bg.finish_idx(tiles) == 1

    def test_empty_board(self):
        assert bg.finish_idx([]) is None


class TestCanTriggerRoll:
    def test_team_mode(self):
        s = bg.board_settings(None)  # manual_roller: team
        assert bg.can_trigger_roll(s, is_team_member=True, is_admin=False) is True
        assert bg.can_trigger_roll(s, is_team_member=False, is_admin=False) is False

    def test_group_admin_mode_blocks_members(self):
        s = bg.board_settings({"movement": {"manual_roller": "group_admin"}})
        assert bg.can_trigger_roll(s, is_team_member=True, is_admin=False) is False
        assert bg.can_trigger_roll(s, is_team_member=False, is_admin=True) is True

    def test_admin_always_allowed(self):
        s = bg.board_settings(None)
        assert bg.can_trigger_roll(s, is_team_member=False, is_admin=True) is True


class TestInstanceDetection:
    def test_flagged_config(self):
        t = SimpleNamespace(config='{"board_instance": true}')
        assert bg._is_board_instance(t) is True

    def test_plain_or_corrupt_config(self):
        assert bg._is_board_instance(SimpleNamespace(config=None)) is False
        assert bg._is_board_instance(SimpleNamespace(config="{bad")) is False
        assert bg._is_board_instance(SimpleNamespace(config='{"kind": "any_of"}')) is False
