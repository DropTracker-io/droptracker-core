"""Unit tests for web_api/task_requirements.py — the "what actually counts"
resolution behind the task requirement list.

The regression this module exists for: a ``pet_collection`` task with a
39-name allow-list rendered as one line of prose ("Any of 39 listed pets"),
so a participant could not tell a qualifying pet from a non-qualifying one.
Every eligible name must come back, icon-resolvable, with the matcher's
non-obvious rules (duplicates don't count, misc pets are opt-in) stated.

Loaded from file paths with the real engine seeded into sys.modules, so the
lazy ``from services.event_engine import ...`` resolves past the conftest
MagicMock stubs (same pattern as test_event_breakdown.py).
"""

import importlib.util
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, *relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine = _load("_event_engine_for_requirements", "services", "event_engine.py")

_saved = sys.modules.get("services.event_engine")
sys.modules["services.event_engine"] = engine
try:
    from web_api.task_requirements import (build_requirements, requirement_spec,
                                           spec_requirement_names)
finally:
    if _saved is None:
        sys.modules.pop("services.event_engine", None)
    else:
        sys.modules["services.event_engine"] = _saved


@pytest.fixture(autouse=True)
def _real_engine_module():
    saved = sys.modules.get("services.event_engine")
    sys.modules["services.event_engine"] = engine
    yield
    if saved is None:
        sys.modules.pop("services.event_engine", None)
    else:
        sys.modules["services.event_engine"] = saved


def _task(**kw):
    base = {"id": 1, "type": "custom", "label": "t", "target": None,
            "target_value": None, "config": None}
    base.update(kw)
    if isinstance(base.get("config"), dict):
        base["config"] = json.dumps(base["config"])
    return base


def _names(spec, group=0):
    return [i["name"] for i in spec["groups"][group]["items"]]


# ── pet_collection: the reported gap ─────────────────────────────────────────

class TestPetCollection:
    def test_explicit_list_names_every_eligible_pet(self):
        pets = ["Baby mole", "Vorki", "Scurry", "Beaver"]
        spec = requirement_spec(_task(type="pet_collection", target_value=3,
                                      config={"pets": pets}))
        assert _names(spec) == sorted(pets)
        assert spec["groups"][0]["mode"] == "any_of"
        assert spec["groups"][0]["need"] == 3
        assert spec["groups"][0]["label"] == "Any 3 of"
        assert spec["summary"] == "Obtain any 3 of these 4 pets"

    def test_category_gate_expands_to_the_live_taxonomy(self):
        spec = requirement_spec(_task(type="pet_collection", target_value=2,
                                      config={"categories": ["skilling"]}))
        names = _names(spec)
        assert "Beaver" in names and "Heron" in names
        assert "Baby mole" not in names

    def test_specific_pet_is_a_counted_goal(self):
        spec = requirement_spec(_task(type="pet_collection", target="baby mole",
                                      target_value=2))
        assert spec["groups"][0]["mode"] == "count"
        assert spec["groups"][0]["items"] == [{"name": "Baby mole", "required": 2}]
        assert spec["summary"] == "Obtain 2× Baby mole"

    def test_duplicate_gate_is_stated(self):
        """The matcher rejects duplicates (_pet_is_new) — invisible in config
        and the #1 "why didn't this count?" question on pet tasks."""
        spec = requirement_spec(_task(type="pet_collection", target_value=3))
        assert any("duplicate" in n.lower() for n in spec["notes"])

    def test_bare_any_pet_notes_the_misc_exclusion(self):
        spec = requirement_spec(_task(type="pet_collection", target_value=3))
        assert any("misc" in n.lower() for n in spec["notes"])
        assert "Chompy chick" not in _names(spec)

    def test_explicit_list_does_not_claim_misc_is_excluded(self):
        """Listing a misc pet is deliberate — the note would be a lie."""
        spec = requirement_spec(_task(type="pet_collection", target_value=1,
                                      config={"pets": ["Chompy chick"]}))
        assert _names(spec) == ["Chompy chick"]
        assert not any("misc" in n.lower() for n in spec["notes"])

    def test_pets_resolve_as_item_icons(self):
        spec = requirement_spec(_task(type="pet_collection", target_value=1,
                                      config={"pets": ["Baby mole", "Vorki"]}))
        items, npcs = spec_requirement_names(spec)
        assert items == {"baby mole", "vorki"}
        assert npcs == set()
        out = build_requirements(spec, {"baby mole": 12646}, {})
        rows = out["groups"][0]["items"]
        assert rows[0]["icon"] == {"type": "item", "id": 12646, "name": "Baby mole"}
        assert rows[1]["icon"]["id"] is None     # degrades to text, not a 404


# ── item_collection shapes ───────────────────────────────────────────────────

class TestItemCollection:
    def test_flat_any_of_keeps_authored_spelling(self):
        """_config_item_entries keys by the normalized name; showing a player
        'twisted bow' instead of 'Twisted bow' reads like a bug."""
        spec = requirement_spec(_task(
            type="item_collection", target_value=2,
            config={"kind": "any_of", "items": ["Twisted bow", "Dragon claws"]}))
        assert set(_names(spec)) == {"Twisted bow", "Dragon claws"}
        assert spec["summary"] == "Collect any 2 from these 2 items"

    def test_all_of_needs_every_item(self):
        spec = requirement_spec(_task(
            type="item_collection",
            config={"kind": "all_of", "items": ["Bandos chestplate", "Bandos tassets"]}))
        assert spec["groups"][0]["mode"] == "all_of"
        assert spec["groups"][0]["need"] == 2
        assert spec["summary"] == "Collect all 2 items"

    def test_single_item_any_of_carries_the_group_need(self):
        spec = requirement_spec(_task(
            type="item_collection", target_value=6000,
            config={"kind": "any_of", "items": ["Vial of blood"]}))
        assert spec["groups"][0]["items"][0]["required"] == 6000

    def test_point_collection_carries_weights(self):
        spec = requirement_spec(_task(
            type="item_collection", target_value=500,
            config={"kind": "point_collection", "items": [
                {"name": "Zenyte shard", "points": 300},
                {"name": "Onyx", "points": 100}]}))
        group = spec["groups"][0]
        assert group["mode"] == "points" and group["unit"] == "pts"
        assert {i["name"]: i["points"] for i in group["items"]} == {
            "Zenyte shard": 300, "Onyx": 100}

    def test_groups_config_yields_one_group_per_requirement(self):
        spec = requirement_spec(_task(type="item_collection", config={
            "kind": "groups", "groups": [
                {"mode": "all_of", "items": ["Godsword shard 1", "Godsword shard 2"]},
                {"mode": "any_of", "need": 1, "items": ["Bandos hilt", "Armadyl hilt"]},
            ]}))
        assert [g["mode"] for g in spec["groups"]] == ["all_of", "any_of"]
        assert spec["groups"][1]["label"] == "Any of"

    def test_any_path_reports_each_alternative(self):
        spec = requirement_spec(_task(type="item_collection", config={
            "kind": "any_path", "paths": [
                {"groups": [{"mode": "all_of", "items": [
                    "Justiciar faceguard", "Justiciar chestguard"]}]},
                {"metric": "kc", "need": 500, "npcs": ["Theatre of Blood"]},
            ]}))
        assert spec["summary"] == "Complete any ONE of these paths"
        assert len(spec["paths"]) == 2
        assert spec["paths"][1]["metric"] == "kc"
        assert spec["paths"][1]["npcs"] == ["Theatre of Blood"]
        _items, npcs = spec_requirement_names(spec)
        assert npcs == {"theatre of blood"}

    def test_source_restriction_is_stated(self):
        spec = requirement_spec(_task(
            type="item_collection", target="Dragon axe",
            config={"source_npcs": ["Dagannoth Rex"]}))
        assert spec["npcs"] == ["Dagannoth Rex"]
        assert any("Dagannoth Rex" in n for n in spec["notes"])


class TestSerializedShape:
    """The payload shape the frontend chips read. An NPC row is
    ``{name, icon}`` (matching the breakdown's metric paths); emitting a bare
    icon ref instead renders every NPC chip icon-less, because the consumer
    reads ``.icon`` and it isn't there."""

    def test_npc_rows_carry_a_nested_icon(self):
        spec = requirement_spec(_task(type="kc_target", target="Zulrah",
                                      target_value=50))
        out = build_requirements(spec, {}, {"zulrah": 2042})
        assert out["npcs"] == [
            {"name": "Zulrah", "icon": {"type": "npc", "id": 2042, "name": "Zulrah"}}]

    def test_metric_path_npcs_use_the_same_shape(self):
        spec = requirement_spec(_task(type="item_collection", config={
            "kind": "any_path",
            "paths": [{"metric": "kc", "need": 500, "npcs": ["Theatre of Blood"]}]}))
        out = build_requirements(spec, {}, {"theatre of blood": 10})
        row = out["paths"][0]["npcs"][0]
        assert row["name"] == "Theatre of Blood"
        assert row["icon"]["id"] == 10

    def test_unresolved_npc_keeps_a_null_id_not_a_missing_icon(self):
        spec = requirement_spec(_task(type="kc_target", target="Nonexistent",
                                      target_value=1))
        out = build_requirements(spec, {}, {})
        assert out["npcs"][0]["icon"]["id"] is None

    def test_item_rows_carry_a_nested_icon_too(self):
        spec = requirement_spec(_task(type="pet_collection", target_value=1,
                                      config={"pets": ["Vorki"]}))
        out = build_requirements(spec, {"vorki": 21992}, {})
        row = out["groups"][0]["items"][0]
        assert row["name"] == "Vorki"
        assert row["icon"] == {"type": "item", "id": 21992, "name": "Vorki"}


# ── metric tasks: no checklist, just a legible target line ───────────────────

class TestMetricTasks:
    def test_kc_target_multi_npc(self):
        spec = requirement_spec(_task(type="kc_target", target="Zulrah",
                                      target_value=250,
                                      config={"npcs": ["Zulrah", "Vorkath"]}))
        assert spec["summary"] == "250 kills at Zulrah / Vorkath"
        assert any("any of these" in n.lower() for n in spec["notes"])

    def test_pb_whole_team_requirement_is_spelled_out(self):
        spec = requirement_spec(_task(type="pb_target", target="Zulrah",
                                      target_value=95,
                                      config={"mode": "whole_team"}))
        assert spec["summary"] == "Beat 1:35 at Zulrah — every team member"

    def test_loot_value_scoped(self):
        spec = requirement_spec(_task(type="loot_value", target_value=50_000_000,
                                      config={"source_npcs": ["Zulrah"]}))
        assert spec["summary"] == "Accumulate 50.00M GP from Zulrah"

    def test_custom_task_says_it_is_manual(self):
        spec = requirement_spec(_task(type="custom", label="Wear full graceful"))
        assert spec["summary"] == "Wear full graceful"
        assert any("organiser" in n.lower() for n in spec["notes"])
