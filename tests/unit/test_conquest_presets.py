"""Unit tests for services/conquest_presets.py — the Gielinor preset map:
coverage of the encounter list, tile layout, troop sizing and that the
output passes the designer's own validation.

The preset imports naming helpers from services.task_generator, so both real
modules are loaded by path past the conftest ``services`` stub.
"""

import importlib.util
import itertools
import math
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tg = _load("services.task_generator", "services", "task_generator.py")
if "services" in sys.modules:
    setattr(sys.modules["services"], "task_generator", tg)
presets = _load("_conquest_presets_ut", "services", "conquest_presets.py")
cq = _load("_conquest_rules_for_presets_ut", "services", "conquest.py")


def _catalog(skip=()):
    rows = []
    for enc in tg.ENCOUNTERS:
        if enc["key"] in skip:
            continue
        rows.append({
            "key": enc["key"], "label": enc["label"], "short": enc.get("short"),
            "unit": enc.get("unit") or "kills", "kc_npcs": list(enc["kc_npcs"]),
            "npc_ids": [1000 + len(rows)], "kph": 30.0,
            "uniques": [{"name": f"{enc['key']} unique {i}", "rate": 0.01}
                        for i in range(2)],
        })
    return rows


class TestPresetCoverage:
    def test_every_encounter_placed_exactly_once(self):
        placed = [k for r in presets.GIELINOR_REGIONS for k in r["tiles"]]
        assert len(placed) == len(set(placed))
        assert set(placed) == {e["key"] for e in tg.ENCOUNTERS}

    def test_region_keys_unique(self):
        keys = [r["key"] for r in presets.GIELINOR_REGIONS]
        assert len(keys) == len(set(keys))


class TestClusterOffsets:
    @pytest.mark.parametrize("n", [1, 2, 3, 5, 7, 8])
    def test_tiles_never_overlap(self, n):
        pts = presets.cluster_offsets(n)
        assert len(pts) == n
        for a, b in itertools.combinations(pts, 2):
            assert math.dist(a, b) >= presets.TILE_SPACING * 0.99


class TestSuggestTroopHours:
    def test_mid_sized_event(self):
        # 3 teams × 20 players × 7 days × 1.5h = 630h over 45 tiles × 20.
        assert presets.suggest_troop_hours(3, 20, 7, 45) == 0.75

    def test_unknown_size_defaults(self):
        assert presets.suggest_troop_hours(0, 0, 7, 45) == presets.DEFAULT_TROOP_HOURS

    def test_huge_event_capped(self):
        assert presets.suggest_troop_hours(4, 300, 30, 45) == 8.0


class TestBuildPresetMap:
    def test_full_map_is_valid(self):
        body, skipped = presets.build_preset_map("gielinor", _catalog())
        assert skipped == []
        assert len(body["regions"]) == 10 and len(body["tiles"]) == 45
        clean, errors = cq.validate_map(body)
        assert errors == []
        assert all(len(t["rules"]) == 2 for t in clean["tiles"])

    def test_tiles_on_canvas_and_apart(self):
        body, _ = presets.build_preset_map("gielinor", _catalog())
        w, h = presets.CANVAS
        pts = [(t["x"] * w, t["y"] * h) for t in body["tiles"]]
        assert all(0 < t["x"] < 1 and 0 < t["y"] < 1 for t in body["tiles"])
        closest = min(math.dist(a, b) for a, b in itertools.combinations(pts, 2))
        assert closest >= presets.TILE_SPACING * 0.95

    def test_troop_sizing_uses_kill_rate(self):
        body, _ = presets.build_preset_map("gielinor", _catalog(), troop_hours=1.0)
        zulrah = next(t for t in body["tiles"] if t["key"] == "zulrah")
        kc = zulrah["rules"][0]["new_task"]
        assert kc["type"] == "kc_target" and kc["target_value"] == 30
        assert zulrah["rules"][1]["troops"] == presets.DEFAULT_UNIQUE_TROOPS
        assert zulrah["rules"][1]["new_task"]["config"]["kind"] == "any_of"

    def test_region_bonus_scales_with_size(self):
        body, _ = presets.build_preset_map("gielinor", _catalog())
        bonus = {r["key"]: r["bonus"] for r in body["regions"]}
        assert bonus["wilderness"] == 4 and bonus["desert"] == 1

    def test_unpriced_encounters_skipped(self):
        body, skipped = presets.build_preset_map(
            "gielinor", _catalog(skip={"yama", "abyssal_sire", "the_leviathan"}))
        assert set(skipped) == {"yama", "abyssal_sire", "the_leviathan"}
        assert "abyss" not in {r["key"] for r in body["regions"]}  # emptied out
        assert len(body["tiles"]) == 42

    def test_no_uniques_means_one_rule(self):
        cat = _catalog()
        for row in cat:
            row["uniques"] = []
        body, _ = presets.build_preset_map("gielinor", cat)
        assert all(len(t["rules"]) == 1 for t in body["tiles"])

    def test_unknown_preset(self):
        with pytest.raises(ValueError):
            presets.build_preset_map("westeros", _catalog())
