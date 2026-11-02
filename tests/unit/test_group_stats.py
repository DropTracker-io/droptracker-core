"""Clan stats (services/group_stats.py, suggestion #224): the pure half.

Periods, the XP table, and the folds that turn rollup rows and Wise Old Man
gains into per-member rows, per-skill/boss rankings and standouts. The SQL half
was checked read-only against production when it was written; these pin the
arithmetic so a refactor can't quietly change a number on the page.

Loaded by path so the conftest ``services`` stub doesn't shadow the module, and
with the real ``services.recap`` swapped in for its period helpers.
"""
import importlib.util
import os
import sys
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *rel))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


recap = _load("_recap_for_group_stats", ("services", "recap.py"))
gs = _load("_group_stats_under_test", ("services", "group_stats.py"))
design = _load("_recap_design_for_group_stats", ("services", "recap_design.py"))
discord_card = _load("_group_stats_discord_under_test", ("services", "group_stats_discord.py"))


@pytest.fixture(autouse=True)
def _real_recap(monkeypatch):
    monkeypatch.setitem(sys.modules, "services.recap", recap)


NOW = datetime(2026, 11, 2, 13, 0)


class TestPeriods:
    def test_a_month(self):
        w = gs.parse_period("2026-10", now=NOW)
        assert w.kind == "month" and w.months == ["2026-10"]
        assert (w.start, w.end) == ("2026-10-01 00:00:00", "2026-11-01 00:00:00")
        assert (w.hour_lo, w.hour_hi) == ("2026-10-01-00", "2026-10-31-23")
        assert not w.in_progress

    def test_the_current_month_is_in_progress(self):
        w = gs.parse_period(None, now=NOW)
        assert w.period == "2026-11" and w.in_progress

    def test_a_year_stops_at_this_month(self):
        w = gs.parse_period("2026", now=NOW)
        assert w.months[0] == "2026-01" and w.months[-1] == "2026-11"
        assert w.in_progress

    def test_a_year_starts_where_tracking_did(self):
        assert gs.parse_period("2024", now=NOW).months == ["2024-10", "2024-11", "2024-12"]

    def test_all_time(self):
        w = gs.parse_period("ALL", now=NOW)
        assert w.kind == "all" and w.start is None

    @pytest.mark.parametrize("bad", ["2026-12", "2027", "2024-09", "2023", "2026-13", "junk", "26-10"])
    def test_the_future_and_nonsense_are_refused(self, bad):
        assert gs.parse_period(bad, now=NOW) is None

    def test_closed_periods_cache_longer_than_open_ones(self):
        assert gs.cache_ttl(gs.parse_period("2026-10", now=NOW)) > gs.cache_ttl(
            gs.parse_period("2026-11", now=NOW))


class TestLevels:
    @pytest.mark.parametrize("xp,level", [(0, 1), (83, 2), (82, 1), (13_034_431, 99),
                                          (200_000_000, 99), (1_154, 10)])
    def test_xp_to_level(self, xp, level):
        assert gs.xp_to_level(xp) == level

    def test_virtual_levels_when_asked(self):
        assert gs.xp_to_level(200_000_000, virtual=True) == 126

    def test_levels_gained(self):
        assert gs.levels_gained(13_000_000, 13_100_000) == 1  # 98 -> 99
        assert gs.levels_gained(13_034_431, 14_000_000) == 0  # past 99

    def test_unranked_start_is_unknown_not_a_jump_from_one(self):
        assert gs.levels_gained(-1, 5_000_000) == 0


class TestMetricKinds:
    def test_kinds(self):
        assert gs.metric_kind("overall") == "skill"
        assert gs.metric_kind("sailing") == "skill"
        assert gs.metric_kind("ehb") == "computed"
        assert gs.metric_kind("clue_scrolls_all") == "activity"
        assert gs.metric_kind("zulrah") == "boss"

    def test_a_boss_wom_adds_later_is_still_a_boss(self):
        assert gs.metric_kind("shellbane_gryphon") == "boss"


class TestFoldGains:
    GAINS = {
        1: {"overall": [2_000_000, 100, 2_000_100], "woodcutting": [987_496, 6_858_735, 7_846_231],
            "zulrah": [12, 100, 112], "vorkath": [3, 5, 8], "ehb": [1.25, 10, 11.25],
            "clue_scrolls_all": [4, 10, 14]},
        2: {"overall": [50_000, 1, 2], "woodcutting": [50_000, 7_000_000, 7_050_000],
            "zulrah": [40, 0, 40]},
    }

    def test_member_totals(self):
        members, _, _ = gs.fold_gains(self.GAINS)
        assert members[1] == {"xp": 2_000_000, "levels": 1, "ehb": 1.25, "ehp": 0.0,
                              "kills": 15, "clues": 4}
        assert members[2]["kills"] == 40

    def test_skill_and_boss_rankings_are_sorted(self):
        _, skills, bosses = gs.fold_gains(self.GAINS)
        assert [r[0] for r in skills["woodcutting"]] == [1, 2]
        assert bosses["zulrah"] == [[2, 40], [1, 12]]

    def test_overall_is_not_a_skill_row(self):
        _, skills, _ = gs.fold_gains(self.GAINS)
        assert "overall" not in skills


class TestFoldHiscores:
    def test_current_totals(self):
        members, skills, bosses = gs.fold_hiscores({
            7: {"skills": {"overall": [109_966_066, 2007], "attack": [13_124_357, 99]},
                "bosses": {"zulrah": 500, "vorkath": 20}, "activities": {"clue_scrolls_all": 202},
                "ehb": 44.04, "ehp": 282.07},
        })
        assert members[7]["total_level"] == 2007 and members[7]["kills"] == 520
        assert members[7]["clues"] == 202
        assert skills["attack"] == [[7, 13_124_357, 99]]
        assert bosses["zulrah"] == [[7, 500]]


class TestFoldItems:
    ROWS = [
        (100, "Twisted bow", 1, 1, 1_500_000_000, 1),
        (100, "Twisted bow", 2, 1, 1_400_000_000, 1),
        (200, "Rune", 1, 5000, 1_000_000, 50),
        (None, "ghost", 1, 1, 9, 1),
    ]

    def test_items_by_value_with_receivers(self):
        items, member_items, member_drops, member_loot = gs.fold_items(self.ROWS)
        assert [i["item_id"] for i in items] == [100, 200]
        assert items[0]["receiver_count"] == 2
        assert items[0]["receivers"][0] == [1, 1_500_000_000, 1]
        assert member_drops == {1: 51, 2: 1}
        assert member_items[1][0][0] == 100
        assert member_loot[1] == 1_501_000_000


class TestStandouts:
    MEMBERS = [
        {"player_id": 1, "loot": 10, "xp": 0, "pets": 2},
        {"player_id": 2, "loot": 30, "xp": 5, "pets": 0},
        {"player_id": 3, "loot": 20, "xp": 9, "pets": 1},
    ]

    def test_top_three_per_category_and_none_for_nobody(self):
        out = gs.build_standouts(self.MEMBERS, [], {}, {})
        by_key = {c["key"]: c for c in out["categories"]}
        assert [e["player_id"] for e in by_key["loot"]["entries"]] == [2, 3, 1]
        assert [e["player_id"] for e in by_key["xp"]["entries"]] == [3, 2]
        assert "deaths" not in by_key and "biggest_drop" not in by_key

    def test_biggest_drop_comes_from_the_drops_list(self):
        out = gs.build_standouts([], [{"player_id": 9, "value": 99, "name": "Tbow", "item_id": 1}], {}, {})
        assert out["categories"][0]["entries"][0]["detail"] == "Tbow"

    def test_skill_leaders_rank_by_clan_total(self):
        out = gs.build_standouts([], [], {"mining": [[1, 10, 0]], "fishing": [[2, 5, 0], [3, 30, 0]]}, {})
        assert [s["metric"] for s in out["skills"]] == ["fishing", "mining"]

    def test_categories_match_the_recap_designer(self):
        # A standout the designer can't name would be unselectable on the card.
        assert gs.STANDOUT_KEYS == design.STANDOUT_KEYS


class TestDiscordCard:
    PAYLOAD = {
        "group": {"id": 363, "name": "Venture Clan"},
        "period": "2026-10",
        "names": {"1": "Buzzyn"},
        "totals": {"members_active": 5, "members_total": 10, "loot": 2_000_000_000, "xp": 39_244_102},
        "standouts": {"categories": [{"key": "loot", "label": "Most loot", "unit": "gp",
                                      "entries": [{"player_id": 1, "value": 417_684_851}]}],
                      "skills": [], "bosses": [{"metric": "tztok_jad", "total": 3,
                                                "entries": [{"player_id": 1, "value": 3}]}]},
        "coverage": {"wom": "stored"},
    }

    def test_the_card(self):
        card = discord_card.build_embed(self.PAYLOAD)
        assert card["title"] == "Venture Clan · October 2026"
        assert card["url"].endswith("/groups/363/stats?period=2026-10")
        assert "**XP** 39.24M" in card["description"]
        assert "Buzzyn, 417.68M gp" in card["fields"][0]["value"]
        assert "TzTok-Jad" in card["fields"][1]["value"]

    def test_no_payload_no_card(self):
        assert discord_card.build_embed({}) is None

    def test_metric_labels(self):
        assert discord_card.metric_label("the_fractured_archive") == "The Fractured Archive"
        assert discord_card.metric_label("chambers_of_xeric") == "Chambers of Xeric"
