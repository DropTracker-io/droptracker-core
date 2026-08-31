"""Region knowledge, and the two copies of it that must not drift.

``utils/death_regions.py`` and ``data/region_names.json`` are both deliberate
duplicates of plugin code: the plugin answers "was this death safe?" and "what
is this place called?" for the client, and the server needs the same answers
for submissions the client could not answer for (pre-6.0 plugins) and for
questions the client is never asked (which areas exist, so a leader can pick
one to blacklist).

A duplicate is only defensible while it is identical, so the parity checks here
are the point of the file. They skip — rather than fail — when the plugin
repository is not checked out beside this one, per ``tests/local_artifacts``.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.local_artifacts import plugin_repo_root, skip_without
from utils import region_names
from utils.death_regions import SAFE_REGIONS, is_safe_region

CASTLE_WARS = 9520
CHAMBERS_OF_XERIC = 12889
CLAN_HALL = 6997
INFERNO = 9043
FIGHT_CAVES = 9551
VORKATH = 9023


def _plugin_file(*parts):
    root = plugin_repo_root()
    skip_without(root is not None, "the sibling plugin repository",
                 "Check it out beside this one, or set DROPTRACKER_PLUGIN_ROOT.")
    path = root.joinpath(*parts)
    skip_without(path.is_file(), f"the plugin's {path.name}")
    return path


class TestSafeRegions:
    @pytest.mark.parametrize(
        "region_id",
        [CASTLE_WARS, CHAMBERS_OF_XERIC, CLAN_HALL, 7512, 9033, 10536, 8493, 13658],
    )
    def test_known_safe_places(self, region_id):
        assert is_safe_region(region_id)

    @pytest.mark.parametrize("region_id", [VORKATH, INFERNO, FIGHT_CAVES, 0, 99999])
    def test_dangerous_and_unknown_places(self, region_id):
        # Inferno and Fight Caves are mechanically safe but deliberately not
        # listed — losing a run there is the whole point of announcing it.
        assert not is_safe_region(region_id)

    @pytest.mark.parametrize("value", [None, "", "N/A", "abc", [], {}])
    def test_unparseable_input_is_not_safe(self, value):
        # This feeds a filter that MUTES safe deaths, so an unreadable region
        # must never be guessed as safe.
        assert not is_safe_region(value)

    def test_string_region_ids_are_accepted(self):
        # Embed fields arrive as strings.
        assert is_safe_region("9520")

    def test_parity_with_the_plugin_classifier(self):
        """The server's fallback must classify exactly what the plugin does."""
        source = _plugin_file("src", "main", "java", "io", "droptracker",
                              "util", "DeathRegions.java").read_text(encoding="utf-8")

        java_regions: set[int] = set()
        for match in re.finditer(
            r"private static final Set<Integer> \w+ = regions\(([^;]*?)\);", source, re.S
        ):
            java_regions |= {int(x) for x in re.findall(r"\d+", match.group(1))}
        for name in ("CLAN_HALL", "CREATURE_GRAVEYARD", "NIGHTMARE_ZONE",
                     "PEST_CONTROL_LANDER", "TZHAAR_FIGHT_PIT"):
            single = re.search(rf"{name} = (\d+);", source)
            assert single, f"{name} missing from DeathRegions.java"
            java_regions.add(int(single.group(1)))

        assert java_regions, "parsed no regions out of DeathRegions.java"
        assert set(SAFE_REGIONS) == java_regions, (
            "utils/death_regions.py has drifted from the plugin's DeathRegions.java: "
            f"only in plugin={sorted(java_regions - set(SAFE_REGIONS))} "
            f"only in server={sorted(set(SAFE_REGIONS) - java_regions)}"
        )


class TestRegionNames:
    def test_resolves_an_area_name(self):
        assert region_names.name_for(CASTLE_WARS) == "Castle Wars"
        assert region_names.name_for(CHAMBERS_OF_XERIC) == "Chambers of Xeric"

    def test_an_unnamed_region_resolves_to_nothing(self):
        # Two of the 97 safe regions have no name; callers show the bare id.
        assert region_names.name_for(CLAN_HALL) is None

    @pytest.mark.parametrize("value", [None, "", "N/A", "abc"])
    def test_unparseable_ids_resolve_to_nothing(self, value):
        assert region_names.name_for(value) is None
        assert region_names.type_for(value) is None

    def test_an_area_maps_back_to_every_region_it_spans(self):
        assert region_names.regions_for("Castle Wars") == {9520, 9620}
        assert len(region_names.regions_for("Chambers of Xeric")) == 14

    def test_area_lookup_is_spelling_insensitive(self):
        for spelling in ("Castle Wars", "castle wars", "  CASTLE_WARS  "):
            assert region_names.regions_for(spelling) == {9520, 9620}

    def test_an_unknown_area_maps_to_nothing(self):
        assert region_names.regions_for("Nowhere In Particular") == set()

    def test_a_name_used_by_two_areas_covers_both(self):
        # "Lighthouse" is filed twice with different region sets; a leader
        # picking it means the place, so both sets must come back.
        lighthouse = region_names.regions_for("Lighthouse")
        assert len(lighthouse) > 1

    def test_every_area_is_listed_for_the_picker(self):
        areas = region_names.all_areas()
        assert len(areas) > 300
        assert all(a["name"] and a["regions"] for a in areas)
        assert {a["type"] for a in areas} <= set(region_names.AREA_TYPES)

    def test_named_regions_cover_almost_every_safe_region(self):
        # The picker is only useful if the places a group wants to mute have
        # names. Two do not (clan hall, one POH chunk) and are muted by id.
        unnamed = [r for r in SAFE_REGIONS if region_names.name_for(r) is None]
        assert len(unnamed) <= 2

    def test_data_file_matches_the_plugin_resource(self):
        """The JSON is a verbatim copy; a regenerated plugin resource must be recopied."""
        plugin_json = _plugin_file("src", "main", "resources", "io", "droptracker",
                                   "region_names.json")
        with open(plugin_json, "r", encoding="utf-8") as handle:
            expected = json.load(handle)
        with open(region_names._DATA_PATH, "r", encoding="utf-8") as handle:
            actual = json.load(handle)

        def _index(payload):
            return {a["name"] + "|" + str(a.get("type")): sorted(a["regions"])
                    for a in payload["areas"]}

        assert _index(actual) == _index(expected), (
            "data/region_names.json has drifted from the plugin resource; "
            "recopy it rather than hand-editing."
        )
