"""Death safety and cost, computed on the server from the plugin's facts.

From plugin 6.2 the client sends what only it can see (account type, Pest
Control overlay, skull, Protect Item, every carried item with its client
price) and the server decides ``is_safe_death`` and the value fields. These
are the plugin's former DeathRegionsTest/DeathValuationTest cases, moved here
with the logic; a 3,000-case randomized comparison against the Java code
matched exactly before it was removed.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dr = _load("_death_regions_real", "utils/death_regions.py")
dv = _load("_death_valuation_real", "utils/death_valuation.py")
PLAIN = 4151


def split(keep, *rows):
    return dv.value([tuple(r) for r in rows], False, keep)


# --------------------------------------------------------------------------- #
# Safe or dangerous
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hardcore", [3, 5])
def test_hardcore_minigame_deaths_are_safe(hardcore):
    for region in (13658, 10588, 9520, 7257):  # LMS, PK tutor, Castle Wars, POH
        assert dr.classify_safe(region, hardcore) is True


def test_hardcore_raid_and_gauntlet_deaths_are_dangerous():
    assert dr.classify_safe(12889, 3) is False  # CoX
    assert dr.classify_safe(7512, 3) is False   # Gauntlet
    assert dr.classify_safe(9023, 3) is False   # Vorkath


def test_other_accounts_follow_the_region():
    assert dr.classify_safe(12889, 1) is True
    assert dr.classify_safe(13658, 1) is True
    assert dr.classify_safe(9023, 1) is False
    assert dr.classify_safe(9043, 0) is False   # Inferno: announced on purpose
    assert dr.classify_safe(9551, 0) is False   # Fight Caves too


def test_a_running_pest_control_game_is_safe_anywhere():
    assert dr.classify_safe(10537, 0, pest_control=True) is True
    assert dr.classify_safe(10537, 0) is False
    assert dr.classify_safe(10536, 3) is True   # the lander itself


def test_an_unreadable_region_is_dangerous():
    assert dr.classify_safe(None) is False and dr.classify_safe("x") is False


# --------------------------------------------------------------------------- #
# What it cost
# --------------------------------------------------------------------------- #
def test_keep_count():
    assert dv.keep_count(0, skulled=False, protect_item=False) == 3
    assert dv.keep_count(0, skulled=False, protect_item=True) == 4
    assert dv.keep_count(0, skulled=True, protect_item=True) == 1
    assert dv.keep_count(2, skulled=False, protect_item=True) == 0  # UIM


def test_protects_the_three_most_valuable_items():
    assert split(3, (PLAIN, 1, 1_000_000), (PLAIN, 1, 500_000), (PLAIN, 1, 100_000), (PLAIN, 1, 10_000)) \
        == (10_000, 1_600_000, 1)


def test_keep_slots_are_per_item_not_per_stack():
    assert split(1, (PLAIN, 100, 1_000)) == (99_000, 1_000, 1)


def test_skulled_player_keeps_nothing():
    assert split(0, (PLAIN, 1, 1_000_000), (PLAIN, 5, 2_000)) == (1_010_000, 0, 2)


def test_bond_is_kept_without_a_slot():
    assert split(1, (13190, 1, 8_000_000), (PLAIN, 1, 1_000_000), (PLAIN, 1, 5_000)) == (5_000, 9_000_000, 1)
    for bond in (13190, 13191, 13192):
        assert split(0, (bond, 1, 8_000_000)) == (0, 8_000_000, 0)


def test_spare_slots_lose_nothing_and_empty_is_nothing():
    assert split(4, (PLAIN, 1, 1_000), (PLAIN, 1, 500)) == (0, 1_500, 0)
    assert split(3) == (0, 0, 0)


def test_worthless_items_do_not_waste_slots():
    rows = [(PLAIN, 20, 0), (PLAIN, 1, 2_000_000), (PLAIN, 1, 1_000_000), (PLAIN, 1, 750_000),
            (PLAIN, 1, 500_000), (PLAIN, 1, 250_000)]
    assert split(4, *rows) == (250_000, 4_250_000, 2)


def test_partial_stack_and_maxed_stack():
    assert split(2, (PLAIN, 3, 100_000)) == (100_000, 200_000, 1)
    assert split(0, (PLAIN, 2_147_483_647, 1)) == (2_147_483_647, 0, 1)


def test_a_safe_death_keeps_everything():
    assert dv.value([(PLAIN, 2, 100), (13190, 1, 5)], True, 0) == (0, 205, 0)


def test_parse_items_skips_garbage():
    assert dv.parse_items("4151:1:2500000,995:1000:1", "N/A", "x:1:2,1:0:5,,7:1") == [(4151, 1, 2_500_000), (995, 1000, 1)]


# --------------------------------------------------------------------------- #
# The processor's expansion
# --------------------------------------------------------------------------- #
@pytest.fixture
def expand(monkeypatch):
    utils_pkg = types.ModuleType("utils")
    utils_pkg.death_valuation = dv
    monkeypatch.setitem(sys.modules, "utils", utils_pkg)
    monkeypatch.setitem(sys.modules, "utils.death_valuation", dv)
    monkeypatch.setitem(sys.modules, "utils.death_regions", dr)
    df = _load("_death_filter_real", "db/death_filter.py")
    src = (ROOT / "data/submissions/death.py").read_text()
    ns = {"parse_flag": df.parse_flag}
    exec(src[src.index("def expand_death_facts"):src.index("async def death_processor")], ns)
    return ns["expand_death_facts"]


def test_expansion_writes_the_fields_the_plugin_used_to_send(expand):
    out = expand({"region_id": "9023", "account_type_id": "0", "skulled": "false", "protect_item": "true",
                  "pest_control": "false", "carried_inv": "4151:1:2500000,995:5000:1", "carried_worn": "N/A"})
    assert (out["is_safe_death"], out["value_lost"], out["value_kept"], out["items_lost"]) == \
        ("false", "4997", "2500003", "1")
    assert out["carried_inv"]  # the facts stay alongside


def test_expansion_leaves_older_plugins_alone(expand):
    old = {"region_id": "9023", "is_safe_death": "true", "value_lost": "5"}
    assert expand(old) is old
    assert expand({"region_id": "9023"}) == {"region_id": "9023"}


def test_an_unlocated_death_is_dangerous(expand):
    assert expand({"carried_inv": "N/A"})["is_safe_death"] == "false"
