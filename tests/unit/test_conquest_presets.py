"""Unit tests for services/conquest_presets.py — the Gielinor preset map:
coverage of the encounter list, tile layout, troop sizing, picking regions
and tiles (and the pre-cut territories that follow), and that the output
passes the designer's own validation.

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


EXTRA_KEYS = {t["key"] for t in presets.EXTRA_TILES}
ALL_TILES = len(tg.ENCOUNTERS) + len(presets.EXTRA_TILES)


def _lookups():
    """What the route would look up for EXTRA_TILES: every name known."""
    names = {n for t in presets.EXTRA_TILES for n in t.get("uniques") or []}
    names |= {t["icon_item"] for t in presets.EXTRA_TILES if t.get("icon_item")}
    pages = {p: [f"{p} item {i}" for i in range(3)]
             for t in presets.EXTRA_TILES for p in t.get("clog_pages") or []}
    sections = {slug: [f"{slug} item {i}" for i in range(3)]
                for t in presets.EXTRA_TILES for slug in t.get("sections") or []}
    names |= {n for v in pages.values() for n in v} | {n for v in sections.values() for n in v}
    return {
        "npc_ids": {n: [5000 + i] for i, n in enumerate(
            sorted({n for t in presets.EXTRA_TILES for n in t.get("kc_npcs") or []}))},
        "wom_rates": {}, "clog_pages": pages, "sections": sections,
        "items": {n.lower(): (n, 20000 + i) for i, n in enumerate(sorted(names))},
    }


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
    return rows + [r for r in presets.resolve_extra_tiles(**_lookups()) if r["key"] not in skip]


def _tile(body, key):
    return next(t for t in body["tiles"] if t["key"] == key)


class TestPresetCoverage:
    def test_every_tile_placed_exactly_once(self):
        placed = [k for r in presets.GIELINOR_REGIONS for k in r["tiles"]]
        assert len(placed) == len(set(placed))
        assert set(placed) == {e["key"] for e in tg.ENCOUNTERS} | EXTRA_KEYS
        assert not EXTRA_KEYS & {e["key"] for e in tg.ENCOUNTERS}

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
        assert len(body["regions"]) == len(presets.GIELINOR_REGIONS)
        assert len(body["tiles"]) == ALL_TILES
        clean, errors = cq.validate_map(body)
        assert errors == []
        assert all(len(t["rules"]) == 2 for t in clean["tiles"] if t["key"] not in EXTRA_KEYS)
        assert all(t["rules"] for t in clean["tiles"])

    def test_rule_troop_cap_matches_the_rules_module(self):
        assert presets.MAX_RULE_TROOPS == cq.MAX_TROOPS_PER_RULE

    def test_tiles_on_canvas_and_apart(self):
        body, _ = presets.build_preset_map("gielinor", _catalog())
        assert all(0 < t["x"] < 1 and 0 < t["y"] < 1 for t in body["tiles"])
        # No two badges (medallion + name scroll) overlap on the drawn map.
        art = presets.preset_art("gielinor")
        b = art["badge"]

        def box(t):
            x, y = t["x"] * art["width"], t["y"] * art["height"]
            half = max((len(t["label"]) * b["char_w"] + b["pad"]) / 2 + b["tail"], b["r"] + 4)
            return (x - half, x + half, y - b["up"], y + b["down"])

        for s1, s2 in itertools.combinations([box(t) for t in body["tiles"]], 2):
            assert s1[1] <= s2[0] or s2[1] <= s1[0] or s1[3] <= s2[2] or s2[3] <= s1[2]

    def test_drawn_map_matches_the_preset(self):
        """The art pack (scripts/conquest_map) and the preset agree on every
        tile and region, so every territory gets drawn."""
        art = presets.preset_art("gielinor")
        body, _ = presets.build_preset_map("gielinor", _catalog())
        assert set(art["tiles"]) == {t["key"] for t in body["tiles"]}
        assert {k: r["name"] for k, r in art["regions"].items()} == {
            r["key"]: r["name"] for r in presets.GIELINOR_REGIONS}
        assert body["art"] == {"background_url": art["background"],
                               "width": art["width"], "height": art["height"]}
        clean, errors = cq.validate_map(body)
        assert errors == []
        assert all(t["shape"] for t in clean["tiles"])
        assert all(r["shape"] for r in clean["regions"])
        assert all(0 < r["label_x"] < 1 and 0 < r["label_y"] < 1 for r in clean["regions"])

    def test_schematic_fallback_without_art(self, monkeypatch):
        monkeypatch.setattr(presets, "preset_art", lambda preset: None)
        body, _ = presets.build_preset_map("gielinor", _catalog())
        assert "art" not in body
        assert all(t["shape"] is None for t in body["tiles"])
        w, h = presets.CANVAS
        pts = [(t["x"] * w, t["y"] * h) for t in body["tiles"]]
        closest = min(math.dist(a, b) for a, b in itertools.combinations(pts, 2))
        assert closest >= presets.TILE_SPACING * 0.95

    def test_slow_kills_pay_several_troops(self):
        """A kill that takes longer than a troop's worth of play pays for the
        time it took, so the Inferno isn't the worst tile on the map."""
        body, _ = presets.build_preset_map("gielinor", _catalog(), troop_hours=0.5)
        zuk = _tile(body, "inferno")["rules"][0]  # 0.8 kills/hour, 0.5h troops
        assert zuk["new_task"]["target_value"] == 1 and zuk["troops"] == 3
        body, _ = presets.build_preset_map("gielinor", _catalog(), troop_hours=8.0)
        assert _tile(body, "inferno")["rules"][0]["troops"] == 1
        body, _ = presets.build_preset_map("gielinor", _catalog(), troop_hours=0.25)
        assert _tile(body, "inferno")["rules"][0]["troops"] == 5

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
        assert bonus["wilderness"] == 4 and bonus["desert"] == 1 and bonus["seas"] == 4

    def test_unpriced_encounters_skipped(self):
        body, skipped = presets.build_preset_map(
            "gielinor", _catalog(skip={"yama", "abyssal_sire", "the_leviathan"}))
        assert set(skipped) == {"yama", "abyssal_sire", "the_leviathan"}
        assert "abyss" not in {r["key"] for r in body["regions"]}  # emptied out
        assert len(body["tiles"]) == ALL_TILES - 3
        # The board has to be redrawn without them: the pick isn't the full map.
        kept, drop, skipped = presets.plan_selection(
            _catalog(skip={"yama", "abyssal_sire", "the_leviathan"}))
        assert "abyss" not in kept and drop == ["yama"]
        assert not presets.is_full_selection(kept, drop)

    def test_no_uniques_means_one_rule(self):
        cat = _catalog()
        for row in cat:
            row["uniques"] = []
        body, skipped = presets.build_preset_map("gielinor", cat)
        assert all(len(t["rules"]) == 1 for t in body["tiles"])
        # Tiles that are nothing but collection-log items have nothing left.
        assert "lost_schematics" in skipped and "fight_caves" not in skipped

    def test_zero_unique_troops_keeps_item_only_tiles(self):
        body, _ = presets.build_preset_map("gielinor", _catalog(), unique_troops=0)
        assert len(_tile(body, "zulrah")["rules"]) == 1
        schematics = _tile(body, "lost_schematics")["rules"]
        assert len(schematics) == 1 and schematics[0]["troops"] == 1


class TestPickRegionsAndTiles:
    def test_only_picked_regions_are_built(self):
        body, skipped = presets.build_preset_map(
            "gielinor", _catalog(), regions=["kourend", "seas"])
        assert [r["key"] for r in body["regions"]] == ["kourend", "seas"]
        assert {t["region_key"] for t in body["tiles"]} == {"kourend", "seas"}
        assert skipped == []  # a region left out isn't "skipped"
        assert cq.validate_map(body)[1] == []

    def test_plan_selection(self):
        kept, drop, skipped = presets.plan_selection(
            _catalog(), regions=["kourend", "seas", "wilderness"],
            exclude=["chambers_of_xeric", "callisto_artio"])
        assert kept == ["kourend", "wilderness", "seas"]      # preset order
        assert drop == ["chambers_of_xeric", "callisto_artio"]
        assert skipped == []
        full = presets.plan_selection(_catalog())
        assert presets.is_full_selection(full[0], full[1])
        assert not presets.is_full_selection(kept, drop)

    def test_uses_the_art_drawn_for_the_pick(self):
        art = presets.preset_art("gielinor")
        mini = {
            "width": 900, "height": 600, "background": "https://cdn.example/x.webp",
            "tiles": {"zulrah": {"label": "Zulrah", "x": 450, "y": 300, "shape": "M1 1l5 0 0 5z"},
                      "corrupted_gauntlet": {"label": "Gauntlet", "x": 90, "y": 60,
                                             "shape": "M2 2l5 0 0 5z"}},
            "regions": {"kandarin": {"label": [300, 200], "shape": "M0 0l9 0 0 9z"}},
            "edges": [["corrupted_gauntlet", "zulrah"], ["kraken", "zulrah"]],
        }
        body, _ = presets.build_preset_map(
            "gielinor", _catalog(), regions=["kandarin"],
            exclude=["kraken", "thermonuclear_smoke_devil", "demonic_gorillas"], art=mini)
        assert [t["key"] for t in body["tiles"]] == ["zulrah", "corrupted_gauntlet"]
        assert _tile(body, "zulrah")["shape"] == "M1 1l5 0 0 5z"
        assert (_tile(body, "zulrah")["x"], _tile(body, "zulrah")["y"]) == (0.5, 0.5)
        assert body["regions"][0]["shape"] == "M0 0l9 0 0 9z"
        # Only connections between tiles that were built.
        assert body["edges"] == [["corrupted_gauntlet", "zulrah"]]
        assert body["art"]["background_url"] == "https://cdn.example/x.webp"
        assert art["width"] != 900  # the committed pack wasn't touched

    def test_region_bonus_follows_the_kept_tiles(self):
        body, _ = presets.build_preset_map(
            "gielinor", _catalog(), exclude=["callisto_artio", "vetion_calvarion",
                                             "venenatis_spindel", "chaos_elemental"])
        assert {r["key"]: r["bonus"] for r in body["regions"]}["wilderness"] == 2

    def test_full_map_is_one_connected_board(self):
        """The committed art connects every territory, and every crossing
        between landmasses goes through a sea tile (seas fill the water)."""
        art = presets.preset_art("gielinor")
        keys = {k for r in presets.GIELINOR_REGIONS for k in r["tiles"]}
        assert set(art["tiles"]) == keys
        adj = cq.adjacency(art["edges"])
        assert {k for e in art["edges"] for k in e} <= keys
        assert all(adj.get(k) for k in keys)
        tiles = [{"id": k, "kind": "normal", "owner_team_id": None} for k in keys]
        assert len(cq.map_parts(tiles, adj)) == 1
        sea = {k for r in presets.GIELINOR_REGIONS if r.get("sea") for k in r["tiles"]}
        # Islands only a boat reaches border nothing but the sea.
        for island in ("vorkath", "dagannoth_kings"):
            assert adj[island] <= sea
        assert "variants" not in art
        body, _ = presets.build_preset_map("gielinor", _catalog())
        assert len(body["edges"]) == len(art["edges"])
        assert cq.validate_map(body)[1] == []

    def test_nothing_picked_builds_nothing(self):
        body, _ = presets.build_preset_map("gielinor", _catalog(), regions=[])
        assert body["regions"] == [] and body["tiles"] == []


class TestExtraTiles:
    def test_rows_resolve_every_rule(self):
        rows = {r["key"]: r for r in presets.resolve_extra_tiles(**_lookups())}
        assert set(rows) == EXTRA_KEYS
        rules = {k: presets.tile_rules(r, 0.5, 2) for k, r in rows.items()}
        # Kill tiles: a kc rule on their NPC.
        assert rules["fight_caves"][0]["new_task"]["type"] == "kc_target"
        assert rules["fight_caves"][0]["new_task"]["target"] == "TzTok-Jad"
        # Slayer: every N tasks, plus the Clan Log's slayer uniques.
        assert [r["new_task"]["type"] for r in rules["karamja_slayer"]] == [
            "slayer_target", "item_collection"]
        # Collection-log pages: one any-of rule, not pinned to any NPC.
        (sea,) = rules["barracuda_trials"]
        assert sea["new_task"]["config"]["kind"] == "any_of"
        assert "item_npcs" not in sea["new_task"]["config"]
        # A boss's uniques are pinned to the boss.
        shell = rules["shellbane_gryphon"][1]["new_task"]["config"]
        assert set(shell["item_npcs"]) == {"Jar of Feathers", "Belle's folly (tarnished)"}
        # Common drops pay half.
        assert rules["ocean_encounters"][0]["troops"] == 1
        assert rules["sea_treasures"][0]["troops"] == 2
        assert all(cq.rule_task_problem(r["new_task"]["type"], r["new_task"].get("config")) is None
                   for rs in rules.values() for r in rs)

    def test_unknown_names_dropped_and_empty_tiles_left_out(self):
        look = _lookups()
        look["items"] = {k: v for k, v in look["items"].items() if not k.startswith("tzhaar item")}
        look["npc_ids"] = {}
        rows = {r["key"]: r for r in presets.resolve_extra_tiles(**look)}
        assert "tzhaar_city" not in rows  # no known item, no rule
        assert "fight_caves" not in rows  # its NPC is unknown here
        assert rows["shellbane_gryphon"]["kc_npcs"] == []  # uniques only
        assert rows["lost_schematics"]["icon_item_id"]

    def test_npc_names_match_whatever_the_case(self):
        look = _lookups()
        look["npc_ids"] = {"Shellbane gryphon": [15010]}
        rows = {r["key"]: r for r in presets.resolve_extra_tiles(**look)}
        assert rows["shellbane_gryphon"]["kc_npcs"] == ["Shellbane gryphon"]
        rule = presets.tile_rules(rows["shellbane_gryphon"], 0.5, 2)[0]["new_task"]
        assert rule["target"] == "Shellbane gryphon"

    def test_wom_rate_wins_over_the_fallback(self):
        look = _lookups()
        look["wom_rates"] = {"tztok_jad": 4.0}
        rows = {r["key"]: r for r in presets.resolve_extra_tiles(**look)}
        assert rows["fight_caves"]["kph"] == 4.0
        assert rows["inferno"]["kph"] == 0.8

    def test_preset_regions_lists_every_tile(self):
        cat = [r for r in _catalog() if r["key"] != "nex"]
        regions = presets.preset_regions("gielinor", cat)
        assert [r["key"] for r in regions] == [r["key"] for r in presets.GIELINOR_REGIONS]
        tiles = {t["key"]: t for r in regions for t in r["tiles"]}
        assert len(tiles) == ALL_TILES
        assert tiles["nex"]["available"] is False and tiles["zulrah"]["available"] is True
        assert tiles["fight_caves"]["icon_item_id"] and tiles["fight_caves"]["icon_npc_id"] is None
        assert next(r for r in regions if r["key"] == "seas")["sea"] is True

    def test_unknown_preset(self):
        with pytest.raises(ValueError):
            presets.build_preset_map("westeros", _catalog())


class TestPhasedPreset:
    def test_boss_tiles_alternate_steady_and_hunt(self):
        body, _ = presets.build_preset_map("gielinor", _catalog(), phases=2)
        zulrah = _tile(body, "zulrah")["rules"]
        assert sorted({r["phase"] for r in zulrah}) == [1, 2]
        by_phase = {p: [r for r in zulrah if r["phase"] == p] for p in (1, 2)}
        kc = {p: next(r for r in rs if r["new_task"]["type"] == "kc_target")
              for p, rs in by_phase.items()}
        uniq = {p: next(r for r in rs if r["new_task"]["type"] == "item_collection")
                for p, rs in by_phase.items()}
        # One phase asks for twice the kills per troop and pays double for uniques.
        steady, hunt = sorted((1, 2), key=lambda p: kc[p]["new_task"]["target_value"])
        assert kc[hunt]["new_task"]["target_value"] >= 2 * kc[steady]["new_task"]["target_value"] - 1
        assert uniq[hunt]["troops"] == 2 * uniq[steady]["troops"]
        assert all(r["new_task"]["label"].endswith(f"(phase {r['phase']})") for r in zulrah)

    def test_uniques_only_tiles_play_every_phase(self):
        body, _ = presets.build_preset_map("gielinor", _catalog(), phases=3)
        assert {r.get("phase", 0) for r in _tile(body, "lost_schematics")["rules"]} == {0}

    def test_four_phases_fit_and_validate(self):
        body, _ = presets.build_preset_map("gielinor", _catalog(), phases=4)
        assert max(len(t["rules"]) for t in body["tiles"]) <= cq.MAX_RULES_PER_TILE
        clean, errors = cq.validate_map(body)
        assert errors == []
        assert {r["phase"] for t in clean["tiles"] for r in t["rules"]} == {0, 1, 2, 3, 4}

    def test_one_phase_is_unchanged(self):
        one, _ = presets.build_preset_map("gielinor", _catalog(), phases=1)
        plain, _ = presets.build_preset_map("gielinor", _catalog())
        assert one == plain
