"""Unit tests for services/conquest.py — the pure Conquest rules core
(troops, dice battles, regions, hold-time scoring, the starting deal and the
designer's map validation).

Loaded straight from the file so the conftest ``services`` stub never shadows
the real module.
"""

import importlib.util
import os
import random
import sys
from datetime import datetime, timedelta

import pytest

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "conquest.py",
)
_spec = importlib.util.spec_from_file_location("_conquest_ut", _PATH)
cq = importlib.util.module_from_spec(_spec)
sys.modules["_conquest_ut"] = cq
_spec.loader.exec_module(cq)

T0 = datetime(2026, 10, 1, 12, 0, 0)


def _settings(**over):
    return cq.conquest_settings(over)


class _FixedRng:
    """Hands out the given faces in order (randint ignores its bounds)."""

    def __init__(self, faces):
        self.faces = list(faces)

    def randint(self, lo, hi):
        return self.faces.pop(0)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class TestSettings:
    def test_defaults(self):
        s = cq.conquest_settings(None)
        assert s == cq.DEFAULT_SETTINGS
        assert s["scoring_mode"] == "hold_time" and s["battle_mode"] == "dice"

    def test_json_string_and_unknown_keys(self):
        s = cq.conquest_settings('{"battle_mode": "attrition", "bogus": 1}')
        assert s["battle_mode"] == "attrition"
        assert "bogus" not in s

    def test_corrupt_json_falls_back(self):
        assert cq.conquest_settings("{nope") == cq.DEFAULT_SETTINGS

    def test_bad_enum_falls_back(self):
        assert cq.conquest_settings({"scoring_mode": "nah"})["scoring_mode"] == "hold_time"

    def test_numbers_clamped(self):
        s = cq.conquest_settings({"attack_dice": 9, "max_defense": 0})
        assert s["attack_dice"] == 3 and s["max_defense"] == 1

    def test_defense_values_capped_at_max(self):
        s = cq.conquest_settings({"max_defense": 3, "capture_defense": 7,
                                  "start_defense": 5, "neutral_defense": 4})
        assert (s["capture_defense"], s["start_defense"], s["neutral_defense"]) == (3, 3, 3)

    def test_summary_hours_choices(self):
        assert cq.conquest_settings({"summary_hours": 12})["summary_hours"] == 12
        assert cq.conquest_settings({"summary_hours": 13})["summary_hours"] == 24


class TestSettingsPatch:
    def test_valid_patch(self):
        patch, errors = cq.clean_settings_patch(
            {"scoring_mode": "final", "attack_dice": 3, "summary_hours": 0})
        assert errors == []
        assert patch == {"scoring_mode": "final", "attack_dice": 3, "summary_hours": 0}

    def test_errors(self):
        patch, errors = cq.clean_settings_patch(
            {"battle_mode": "chess", "max_defense": 99, "who": 1, "attack_dice": True})
        assert patch == {}
        assert len(errors) == 4

    def test_not_an_object(self):
        assert cq.clean_settings_patch([1]) == ({}, ["Settings must be an object."])


# --------------------------------------------------------------------------- #
# Troops
# --------------------------------------------------------------------------- #
class TestTroopsForProgress:
    def test_crossing_one_multiple(self):
        assert cq.troops_for_progress(30, 36, 35) == 1

    def test_not_crossing(self):
        assert cq.troops_for_progress(1, 34, 35) == 0

    def test_big_jump_crosses_several(self):
        # A WOM fold of 110 kills from 0 at 35/troop.
        assert cq.troops_for_progress(0, 110, 35) == 3

    def test_troops_per_multiplies(self):
        assert cq.troops_for_progress(0, 1, 1, troops_per=2) == 2

    def test_revoke_is_negative(self):
        assert cq.troops_for_progress(36, 30, 35) == -1

    def test_bad_threshold_means_one(self):
        assert cq.troops_for_progress(0, 3, 0) == 3

    def test_progress_to_next(self):
        assert cq.progress_to_next(58, 35) == (23, 35)


# --------------------------------------------------------------------------- #
# Battles
# --------------------------------------------------------------------------- #
class TestBattleLosses:
    def test_ties_go_to_defender(self):
        assert cq.battle_losses([5, 3], [5]) == 0

    def test_two_v_one(self):
        assert cq.battle_losses([6, 1], [5]) == 1

    def test_two_v_two_split(self):
        assert cq.battle_losses([6, 2], [5, 4]) == 1

    def test_two_v_two_sweep(self):
        assert cq.battle_losses([6, 5], [4, 4]) == 2

    def test_unsorted_input(self):
        assert cq.battle_losses([2, 6], [4, 5]) == 1


class TestResolveTroop:
    def test_fortify_own_tile(self):
        r = cq.resolve_troop(1, 2, 1, _settings(), None)
        assert (r.outcome, r.defense_after, r.owner_after) == ("fortify", 3, 1)

    def test_full_at_max(self):
        r = cq.resolve_troop(1, 5, 1, _settings(), None)
        assert (r.outcome, r.defense_after) == ("full", 5)

    def test_claim_neutral(self):
        r = cq.resolve_troop(None, 0, 7, _settings(), None)
        assert (r.outcome, r.owner_after, r.defense_after) == ("claim", 7, 1)
        assert r.captured

    def test_capture_open_rival_tile(self):
        r = cq.resolve_troop(2, 0, 7, _settings(capture_defense=0), None)
        assert (r.outcome, r.owner_before, r.owner_after, r.defense_after) == \
            ("capture", 2, 7, 0)

    def test_attrition(self):
        s = _settings(battle_mode="attrition")
        assert cq.resolve_troop(2, 3, 7, s, None).outcome == "attack"
        r = cq.resolve_troop(2, 1, 7, s, None)
        assert (r.outcome, r.defense_after, r.owner_after) == ("breach", 0, 2)

    def test_dice_repelled(self):
        r = cq.resolve_troop(2, 1, 7, _settings(), _FixedRng([4, 2, 6]))
        assert r.outcome == "repelled"
        assert r.attack_dice == (4, 2) and r.defense_dice == (6,)
        assert r.defense_after == 1 and r.owner_after == 2

    def test_dice_breach(self):
        r = cq.resolve_troop(2, 1, 7, _settings(), _FixedRng([6, 1, 5]))
        assert (r.outcome, r.defense_after, r.owner_after) == ("breach", 0, 2)

    def test_dice_attack_two_v_two(self):
        r = cq.resolve_troop(2, 3, 7, _settings(), _FixedRng([6, 2, 5, 4]))
        assert (r.outcome, r.defense_after) == ("attack", 2)
        assert len(r.defense_dice) == 2

    def test_defender_dice_capped_by_defense(self):
        r = cq.resolve_troop(2, 1, 7, _settings(defense_dice=3), _FixedRng([6, 5, 1]))
        assert len(r.defense_dice) == 1

    def test_neutral_garrison_fights(self):
        r = cq.resolve_troop(None, 2, 7, _settings(neutral_defense=2),
                             _FixedRng([1, 1, 6, 6]))
        assert r.outcome == "repelled" and r.owner_after is None


class TestResolveTroops:
    def test_claim_then_fortify(self):
        out = cq.resolve_troops(None, 0, 3, 3, _settings(), None)
        assert [r.outcome for r in out] == ["claim", "fortify", "fortify"]
        assert out[-1].defense_after == 3

    def test_breach_then_capture(self):
        rng = _FixedRng([6, 6, 1])  # the breach roll; the capture needs no dice
        out = cq.resolve_troops(2, 1, 7, 2, _settings(), rng)
        assert [r.outcome for r in out] == ["breach", "capture"]
        assert out[-1].owner_after == 7 and out[-1].defense_after == 1

    def test_zero_troops(self):
        assert cq.resolve_troops(None, 0, 3, 0, _settings(), None) == []

    def test_expected_troops_match_the_published_odds(self):
        # Seeded Monte Carlo of "troops to take a defense-1 tile" with two
        # attack dice: 1/0.5787 rolls to breach + 1 capture = 2.73.
        rng = random.Random(1234)
        s = _settings()
        total = 0
        trials = 4000
        for _ in range(trials):
            owner, defense, n = 2, 1, 0
            while owner != 9:
                r = cq.resolve_troop(owner, defense, 9, s, rng)
                owner, defense, n = r.owner_after, r.defense_after, n + 1
            total += n
        assert total / trials == pytest.approx(2.73, abs=0.08)


# --------------------------------------------------------------------------- #
# Regions
# --------------------------------------------------------------------------- #
def _tile(tid, region=1, owner=None, value=1, kind="normal"):
    return {"id": tid, "region_id": region, "owner_team_id": owner,
            "value": value, "kind": kind}


class TestRegions:
    def test_region_owner(self):
        assert cq.region_owner([1, 1, 1]) == 1
        assert cq.region_owner([1, 2]) is None
        assert cq.region_owner([None, None]) is None
        assert cq.region_owner([]) is None

    def test_respawn_tiles_do_not_count(self):
        tiles = [_tile(1, owner=5), _tile(2, owner=5), _tile(3, kind="respawn")]
        assert cq.region_owners(tiles) == {1: 5}

    def test_unregioned_tiles_ignored(self):
        assert cq.region_owners([_tile(1, region=None, owner=5)]) == {}


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
class TestComputeStandings:
    def _map(self):
        tiles = [_tile(1, owner=10, value=2), _tile(2, owner=10), _tile(3, region=2, owner=20)]
        regions = [{"id": 1, "bonus": 5}, {"id": 2, "bonus": 3}]
        return tiles, regions

    def test_final_mode_is_current_holding(self):
        tiles, regions = self._map()
        st = cq.compute_standings("final", tiles, regions, [], [10, 20, 30], None, None)
        assert st[10].score == 8  # tiles 2 + 1, region 5
        assert (st[10].tiles, st[10].regions) == (2, 1)
        assert st[20].score == 4  # tile 1, region 3
        assert st[30].score == 0

    def test_hold_time_tiles_and_region(self):
        tiles, regions = self._map()
        holds = [
            cq.Hold(1, 10, T0, None),                         # 10h at value 2
            cq.Hold(2, 10, T0 + timedelta(hours=4), None),    # 6h at value 1
            cq.Hold(3, 20, T0, T0 + timedelta(hours=5)),      # 5h at value 1
        ]
        st = cq.compute_standings("hold_time", tiles, regions, holds, [10, 20],
                                  T0, T0 + timedelta(hours=10))
        # Team 10: 2*10 + 1*6 + region(both tiles) 5*6 = 56.
        assert st[10].score == 56
        # Team 20: 1*5 + region 2 (its only tile) 3*5 = 20.
        assert st[20].score == 20
        # Holding = what it earns per hour now: 2 + 1 + 5.
        assert st[10].holding == 8

    def test_hold_time_clips_to_window(self):
        tiles = [_tile(1, owner=10)]
        holds = [cq.Hold(1, 10, T0 - timedelta(hours=5), T0 + timedelta(hours=2))]
        st = cq.compute_standings("hold_time", tiles, [], holds, [10],
                                  T0, T0 + timedelta(hours=1))
        assert st[10].score == 1

    def test_region_needs_simultaneous_hold(self):
        # Team 10 held tile 1 then tile 2, never both at once: no region time.
        tiles = [_tile(1), _tile(2)]
        holds = [cq.Hold(1, 10, T0, T0 + timedelta(hours=1)),
                 cq.Hold(2, 10, T0 + timedelta(hours=1), T0 + timedelta(hours=2))]
        st = cq.compute_standings("hold_time", tiles, [{"id": 1, "bonus": 100}], holds,
                                  [10], T0, T0 + timedelta(hours=2))
        assert st[10].score == 2

    def test_no_window_means_nothing_accrued(self):
        tiles, regions = self._map()
        st = cq.compute_standings("hold_time", tiles, regions,
                                  [cq.Hold(1, 10, T0, None)], [10], None, None)
        assert st[10].score == 0 and st[10].tiles == 2

    def test_unknown_team_holds_ignored(self):
        st = cq.compute_standings("hold_time", [_tile(1)], [], [cq.Hold(1, 99, T0, None)],
                                  [10], T0, T0 + timedelta(hours=1))
        assert st[10].score == 0 and 99 not in st

    def test_rank_teams_tiebreaks(self):
        st = {
            1: cq.TeamStanding(1, score=10, tiles=2),
            2: cq.TeamStanding(2, score=10, tiles=3),
            3: cq.TeamStanding(3, score=12),
            4: cq.TeamStanding(4, score=10, tiles=3, regions=1),
        }
        assert cq.rank_teams(st) == [3, 4, 2, 1]


# --------------------------------------------------------------------------- #
# Starting deal
# --------------------------------------------------------------------------- #
class TestDealTiles:
    def test_every_tile_dealt_and_counts_balanced(self):
        tiles = [(i, 1) for i in range(60)]
        out = cq.deal_tiles(tiles, [1, 2, 3, 4], random.Random(7))
        assert set(out) == set(range(60))
        counts = {t: list(out.values()).count(t) for t in (1, 2, 3, 4)}
        assert set(counts.values()) == {15}

    def test_uneven_counts_within_one(self):
        out = cq.deal_tiles([(i, 1) for i in range(10)], [1, 2, 3], random.Random(1))
        counts = sorted(list(out.values()).count(t) for t in (1, 2, 3))
        assert counts == [3, 3, 4]

    def test_values_balanced_by_snake(self):
        tiles = [(i, v) for i, v in enumerate([10, 9, 8, 7, 6, 5, 4, 3])]
        out = cq.deal_tiles(tiles, [1, 2], random.Random(3))
        value = {tid: v for tid, v in tiles}
        sums = sorted(sum(value[t] for t, team in out.items() if team == k) for k in (1, 2))
        assert sums[1] - sums[0] <= 2

    def test_seeded_is_deterministic(self):
        tiles = [(i, 1) for i in range(12)]
        a = cq.deal_tiles(tiles, [1, 2], random.Random(5))
        b = cq.deal_tiles(tiles, [1, 2], random.Random(5))
        assert a == b

    def test_no_teams(self):
        assert cq.deal_tiles([(1, 1)], [], random.Random(1)) == {}


# --------------------------------------------------------------------------- #
# Map validation
# --------------------------------------------------------------------------- #
def _good_map():
    return {
        "regions": [{"key": "w", "name": "Wilderness", "bonus": 5, "color": "#AA3322"}],
        "tiles": [
            {"key": "t1", "label": "Callisto", "x": 0.5, "y": 0.2, "region_key": "w",
             "rules": [{"task_id": 11, "troops": 1},
                       {"new_task": {"type": "item_collection"}, "troops": 2}]},
            {"key": "t2", "label": "Respawn", "x": 0.1, "y": 0.9, "kind": "respawn"},
        ],
    }


class TestValidateMap:
    def test_good_map(self):
        clean, errors = cq.validate_map(_good_map())
        assert errors == []
        assert clean["regions"][0]["color"] == "#aa3322"
        assert clean["tiles"][0]["rules"][1]["troops"] == 2
        assert clean["tiles"][1]["kind"] == "respawn"

    @pytest.mark.parametrize("mutate, fragment", [
        (lambda m: m["tiles"][0].update(x=1.5), "x and y"),
        (lambda m: m["tiles"][0].update(region_key="nope"), "unknown region"),
        (lambda m: m["tiles"][1].update(rules=[{"task_id": 1}]), "respawn"),
        (lambda m: m["tiles"][0]["rules"][0].update(troops=11), "troops"),
        (lambda m: m["tiles"][0]["rules"][0].update(new_task={}), "exactly one"),
        (lambda m: m["tiles"][1].update(key="t1"), "unique key"),
        (lambda m: m["regions"].append({"key": "w", "name": "Dup"}), "used twice"),
        (lambda m: m["regions"][0].update(bonus=-1), "bonus"),
    ])
    def test_errors(self, mutate, fragment):
        body = _good_map()
        mutate(body)
        _clean, errors = cq.validate_map(body)
        assert any(fragment in e for e in errors), errors

    def test_rule_task_problem(self):
        assert cq.rule_task_problem("kc_target", {}) is None
        assert cq.rule_task_problem("item_collection", {"kind": "any_of"}) is None
        assert "counts up" in cq.rule_task_problem("pb_target", {})
        assert "once" in cq.rule_task_problem("item_collection", {"kind": "all_of"})


# --------------------------------------------------------------------------- #
# Display helpers
# --------------------------------------------------------------------------- #
class TestDisplay:
    def test_headlines_have_no_em_dash(self):
        for outcome in cq.OUTCOMES:
            line = cq.outcome_headline(outcome, team="Red", tile="Zulrah", owner="Blue")
            assert "—" not in line and "Red" in line

    def test_capture_names_previous_owner(self):
        line = cq.outcome_headline("capture", team="Red", tile="Zulrah", owner="Blue")
        assert "from **Blue**" in line

    def test_battle_detail(self):
        r = cq.TroopOutcome("attack", 2, 2, 3, 2, (6, 2), (5, 4))
        assert cq.battle_detail(r, defender="Blue") == (
            "\U0001F3B2 `6 · 2` vs `5 · 4` (Blue), defense 3 → 2")
        assert cq.battle_detail(cq.TroopOutcome("claim", None, 1, 0, 1)) is None

    def test_fmt_points(self):
        assert cq.fmt_points(1234.0) == "1,234"
        assert cq.fmt_points(12.34) == "12.3"
