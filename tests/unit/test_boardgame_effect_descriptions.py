"""Player-facing effect descriptions (services/boardgame_effects.describe_effect).

Every shop surface — the organiser's per-event config, the buy list and the
team's bag — reads its explanation from this one generator, so an item's rules
are stated in exactly one place. The property that matters is that the
sentence tracks the RESOLVED behavior: an event that re-tunes a freeze to 3
turns must stop advertising 2. (The catalog's static ``description`` column is
a superadmin's flavour text and cannot do that, which is why it isn't the
only thing shown.)

The module is a pure leaf (no db/service imports), so it loads by dotted name
straight through the conftest stubs.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parent.parent.parent / "services"


def _load(dotted, filename):
    spec = importlib.util.spec_from_file_location(dotted, _BASE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


fx = _load("services.boardgame_effects", "boardgame_effects.py")


class TestEveryEffectIsExplained:
    def test_no_effect_ships_without_a_description(self):
        """A registered effect with no ``describe`` is an item that would sit
        in the shop with no explanation — the exact gap this closes."""
        missing = [k for k in fx.EFFECT_REGISTRY if not fx.describe_effect(k)]
        assert missing == []

    def test_descriptions_are_prose(self):
        for key in fx.EFFECT_REGISTRY:
            text = fx.describe_effect(key)
            assert text[0].isupper(), key
            assert text.endswith("."), key

    def test_every_targeting_value_is_known(self):
        for key in fx.EFFECT_REGISTRY:
            assert fx.effect_targeting(key) in fx.TARGETING, key

    def test_unknown_effect_degrades_quietly(self):
        assert fx.describe_effect("not_a_real_effect") == ""
        assert fx.effect_targeting("not_a_real_effect") == "self"


class TestDescriptionsTrackTunedBehavior:
    def test_freeze_reports_the_event_s_turn_count(self):
        assert "next 2 rolls" in fx.describe_effect("freeze_opponent")
        assert "next 5 rolls" in fx.describe_effect("freeze_opponent", {"turns": 5})

    def test_freeze_singular_for_one_turn(self):
        assert "next 1 roll " in fx.describe_effect("freeze_opponent", {"turns": 1})

    def test_knockback_reports_the_tile_count(self):
        assert "back 3 tiles" in fx.describe_effect("knockback")
        assert "back 7 tiles" in fx.describe_effect("knockback", {"tiles": 7})

    def test_coin_toll_reports_the_per_team_charge(self):
        assert "25 coins" in fx.describe_effect("coin_toll")
        assert "90 coins" in fx.describe_effect("coin_toll", {"coins_per_team": 90})

    def test_boost_coins_reports_the_multiplier(self):
        assert "by 2×" in fx.describe_effect("boost_coins")
        assert "by 3×" in fx.describe_effect("boost_coins", {"multiplier": 3})

    def test_extra_dice_pluralises(self):
        assert "1 extra die" in fx.describe_effect("extra_dice")
        assert "2 extra dice" in fx.describe_effect("extra_dice", {"extra_dice": 2})

    def test_choose_task_reports_the_candidate_count(self):
        assert "Draws 3 candidate" in fx.describe_effect("choose_task")
        assert "Draws 2 candidate" in fx.describe_effect("choose_task", {"candidates": 2})

    def test_reroll_task_states_the_difficulty_shift(self):
        assert "same tier" in fx.describe_effect("reroll_task")
        assert "1 tier easier" in fx.describe_effect("reroll_task", {"difficulty_shift": -1})
        assert "1 tier harder" in fx.describe_effect("reroll_task", {"difficulty_shift": 1})


class TestRoadblockDescription:
    def test_states_the_stall_and_that_the_placer_is_not_immune(self):
        text = fx.describe_effect("roadblock")
        assert "including your own" in text      # no placer immunity
        assert "loses 1 turn" in text

    def test_no_stall_omits_the_turn_loss_clause(self):
        text = fx.describe_effect("roadblock", {"stall_turns": 0})
        assert "loses" not in text

    def test_break_on_land_is_spelled_out(self):
        text = fx.describe_effect("roadblock", {"break_on": "land"})
        assert "lands exactly on it" in text

    def test_hidden_traps_say_so(self):
        text = fx.describe_effect("roadblock", {"visible_to_all": False})
        assert "cannot see it" in text
        assert "cannot see it" not in fx.describe_effect("roadblock")


class TestWardDescription:
    def test_default_ward_blocks_everything_offensive(self):
        assert "next offensive item" in fx.describe_effect("ward")

    def test_narrow_ward_names_what_it_blocks(self):
        """The rat-poison-style ward only stops item theft — advertising it as
        a general defense would be a lie a player pays coins for."""
        text = fx.describe_effect("ward", {"blocks": ["steal_item"]})
        assert "steal item" in text
        assert "offensive" not in text


class TestBadInputIsSurvivable:
    def test_corrupt_knobs_fall_back_to_defaults(self):
        assert "back 3 tiles" in fx.describe_effect("knockback", {"tiles": "lots"})
        assert "next 2 rolls" in fx.describe_effect("freeze_opponent", {"turns": None})

    def test_a_description_never_raises(self):
        """A shop must not 500 because an effect_config is malformed."""
        for key in fx.EFFECT_REGISTRY:
            assert isinstance(fx.describe_effect(key, {"stall_turns": object()}), str)


class TestTargeting:
    @pytest.mark.parametrize("key,expected", [
        ("freeze_opponent", "team"),
        ("steal_item", "team"),
        ("knockback", "team"),
        ("reroll_opponent_task", "team"),
        ("roadblock", "tile"),
        ("choose_roll", "value"),
        ("shield", "self"),
        ("boost_coins", "self"),
    ])
    def test_matches_what_the_use_form_asks_for(self, key, expected):
        assert fx.effect_targeting(key) == expected
