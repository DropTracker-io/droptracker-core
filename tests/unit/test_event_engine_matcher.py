"""Unit tests for the pure matcher layer of services/event_engine.py (Task 17).

The engine module is loaded directly from its file path (its module-level
imports are stdlib + sqlalchemy.exc only) so the conftest sys.modules stubs
for db/redis/services never interfere.
"""

import importlib.util
import os
import sys

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_under_test", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_under_test"] = engine  # dataclass needs the registry
_spec.loader.exec_module(engine)


def _task(**kw):
    base = {
        "id": 1, "event_id": 10, "type": "item_collection", "label": "t",
        "target": None, "target_value": None, "points": 0,
        "requires_confirmation": False, "config": {},
    }
    base.update(kw)
    return base


def _env(kind, data, guid="g-1", player_id=5, ts=1751600000):
    return {"v": 1, "kind": kind, "guid": guid, "player_id": player_id,
            "ts": ts, "data": data}


# ── item_collection ───────────────────────────────────────────────────────────

class TestItemCollection:
    def test_exact_target_match_drop(self):
        t = _task(target="Abyssal whip", target_value=1)
        m = engine.match_task(t, _env("drop", {"item_name": "Abyssal whip", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1}

    def test_case_and_whitespace_insensitive(self):
        t = _task(target="Abyssal Whip")
        m = engine.match_task(t, _env("drop", {"item_name": "  abyssal   WHIP ", "quantity": 2}))
        assert m == {"mode": "count", "quantity": 2}

    def test_clog_matches_with_quantity_one(self):
        t = _task(target="Dragon pickaxe")
        m = engine.match_task(t, _env("clog", {"item_name": "dragon pickaxe", "kc": 100}))
        assert m == {"mode": "count", "quantity": 1}

    def test_non_matching_item(self):
        t = _task(target="Abyssal whip")
        assert engine.match_task(t, _env("drop", {"item_name": "Dragon whip"})) is None

    def test_wrong_kind_no_match(self):
        t = _task(target="Abyssal whip")
        assert engine.match_task(t, _env("pb", {"item_name": "Abyssal whip"})) is None

    def test_any_of_config_list_of_strings(self):
        t = _task(config={"kind": "any_of", "any_of": ["Bandos chestplate", "Bandos tassets"]})
        m = engine.match_task(t, _env("drop", {"item_name": "bandos tassets", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1}

    def test_all_of_config_items_best_effort(self):
        t = _task(config={"kind": "all_of", "items": [
            {"item_name": "Ahrim's staff", "quantity": 1},
            {"item_name": "Ahrim's hood", "quantity": 1},
        ]}, target_value=2)
        m = engine.match_task(t, _env("drop", {"item_name": "AHRIM'S HOOD", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1}

    def test_point_collection_credits_item_points(self):
        t = _task(config={"kind": "point_collection", "items": [
            {"item_name": "Ahrim's staff", "points": 2.0},
            {"item_name": "Dharok's greataxe", "points": 4.5},
        ]}, target_value=10)
        m = engine.match_task(t, _env("drop", {"item_name": "dharok's greataxe", "quantity": 2}))
        assert m == {"mode": "count", "quantity": 9}  # round(4.5 * 2)

    def test_assembly_config_matches_listed_items(self):
        t = _task(config={"kind": "assembly", "items": [{"item_name": "Godsword shard 1"}]})
        m = engine.match_task(t, _env("drop", {"item_name": "Godsword shard 1"}))
        assert m == {"mode": "count", "quantity": 1}

    def test_drop_quantity_folded(self):
        t = _task(target="Cannonball")
        m = engine.match_task(t, _env("drop", {"item_name": "Cannonball", "quantity": 250}))
        assert m == {"mode": "count", "quantity": 250}


# ── kc_target ─────────────────────────────────────────────────────────────────

class TestKcTarget:
    def test_matching_npc_drop(self):
        t = _task(type="kc_target", target="Zulrah", target_value=50)
        m = engine.match_task(t, _env("drop", {"item_name": "x", "npc_name": "zulrah", "kill_count": 12}))
        assert m == {"mode": "kc", "quantity": 1}

    def test_wrong_npc(self):
        t = _task(type="kc_target", target="Zulrah", target_value=50)
        assert engine.match_task(t, _env("drop", {"npc_name": "Vorkath"})) is None

    def test_clog_does_not_count_kc(self):
        t = _task(type="kc_target", target="Zulrah", target_value=50)
        assert engine.match_task(t, _env("clog", {"npc_name": "Zulrah"})) is None

    def test_missing_target_never_matches(self):
        t = _task(type="kc_target", target=None, target_value=50)
        assert engine.match_task(t, _env("drop", {"npc_name": ""})) is None


# ── pb_target ─────────────────────────────────────────────────────────────────

class TestPbTarget:
    def test_time_under_target_matches(self):
        t = _task(type="pb_target", target="Zulrah", target_value=60)  # 60s
        m = engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 59_400}))
        assert m == {"mode": "first", "quantity": 1}

    def test_time_equal_target_matches(self):
        t = _task(type="pb_target", target="Zulrah", target_value=60)
        m = engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 60_000}))
        assert m == {"mode": "first", "quantity": 1}

    def test_time_over_target_no_match(self):
        t = _task(type="pb_target", target="Zulrah", target_value=60)
        assert engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 60_001})) is None

    def test_zero_time_no_match(self):
        t = _task(type="pb_target", target="Zulrah", target_value=60)
        assert engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 0})) is None

    def test_no_target_value_no_match(self):
        t = _task(type="pb_target", target="Zulrah", target_value=None)
        assert engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 1000})) is None


# ── xp_target / skill_target ──────────────────────────────────────────────────

class TestExperienceTargets:
    def test_xp_target_matches_skill(self):
        t = _task(type="xp_target", target="Slayer", target_value=1_000_000)
        m = engine.match_task(t, _env("experience", {"skill": "slayer", "xp": 5_000_000, "level": 90}))
        assert m == {"mode": "xp", "quantity": 0}

    def test_xp_target_wrong_skill(self):
        t = _task(type="xp_target", target="Slayer", target_value=1_000_000)
        assert engine.match_task(t, _env("experience", {"skill": "attack", "xp": 1})) is None

    def test_skill_target_level_reached(self):
        t = _task(type="skill_target", target="Agility", target_value=90)
        m = engine.match_task(t, _env("experience", {"skill": "Agility", "xp": 1, "level": 90}))
        assert m == {"mode": "first", "quantity": 1}

    def test_skill_target_level_below(self):
        t = _task(type="skill_target", target="Agility", target_value=90)
        assert engine.match_task(t, _env("experience", {"skill": "Agility", "level": 89})) is None

    def test_skill_target_ignores_drops(self):
        t = _task(type="skill_target", target="Agility", target_value=90)
        assert engine.match_task(t, _env("drop", {"skill": "Agility", "level": 99})) is None


# ── loot_value ────────────────────────────────────────────────────────────────

class TestLootValue:
    def test_any_source_folds_total_value(self):
        t = _task(type="loot_value", target_value=10_000_000)
        m = engine.match_task(t, _env("drop", {"item_name": "Coins", "npc_name": "Zulrah",
                                               "total_value": 250_000}))
        assert m == {"mode": "count", "quantity": 250_000}

    def test_target_npc_scopes_credit(self):
        t = _task(type="loot_value", target="Zulrah", target_value=10_000_000)
        env = {"item_name": "x", "npc_name": "Vorkath", "total_value": 100}
        assert engine.match_task(t, _env("drop", env)) is None
        env["npc_name"] = "zulrah"
        assert engine.match_task(t, _env("drop", env)) == {"mode": "count", "quantity": 100}

    def test_config_source_npcs_scope(self):
        t = _task(type="loot_value", target_value=1_000,
                  config={"source_npcs": ["Zulrah", "Vorkath"]})
        assert engine.match_task(t, _env("drop", {"npc_name": "Vorkath", "total_value": 5})) \
            == {"mode": "count", "quantity": 5}
        assert engine.match_task(t, _env("drop", {"npc_name": "Kraken", "total_value": 5})) is None

    def test_zero_value_and_wrong_kind_no_match(self):
        t = _task(type="loot_value", target_value=1_000)
        assert engine.match_task(t, _env("drop", {"npc_name": "Zulrah", "total_value": 0})) is None
        assert engine.match_task(t, _env("clog", {"npc_name": "Zulrah", "total_value": 50})) is None


# ── non-evaluated types ───────────────────────────────────────────────────────

class TestManualOnlyTypes:
    def test_ehp_ehb_custom_never_match(self):
        for task_type in ("ehp_target", "ehb_target", "custom"):
            t = _task(type=task_type, target="anything", target_value=1)
            for kind in ("drop", "pb", "clog", "ca", "experience"):
                assert engine.match_task(t, _env(kind, {"item_name": "anything",
                                                        "npc_name": "anything",
                                                        "skill": "anything",
                                                        "level": 99})) is None


# ── thresholds & config parsing ───────────────────────────────────────────────

class TestHelpers:
    def test_threshold_defaults_to_one(self):
        assert engine.completion_threshold(_task(target_value=None)) == 1
        assert engine.completion_threshold(_task(target_value=0)) == 1

    def test_threshold_uses_target_value(self):
        assert engine.completion_threshold(_task(type="kc_target", target_value=50)) == 50
        assert engine.completion_threshold(_task(type="xp_target", target_value=13_034_431)) == 13_034_431

    def test_first_match_types_threshold_one(self):
        assert engine.completion_threshold(_task(type="pb_target", target_value=60)) == 1
        assert engine.completion_threshold(_task(type="skill_target", target_value=99)) == 1

    def test_parse_task_config_variants(self):
        assert engine.parse_task_config(None) == {}
        assert engine.parse_task_config("") == {}
        assert engine.parse_task_config("not json") == {}
        assert engine.parse_task_config('{"kind": "any_of"}') == {"kind": "any_of"}
        assert engine.parse_task_config({"kind": "all_of"}) == {"kind": "all_of"}
        assert engine.parse_task_config("[1, 2]") == {}

    def test_item_match_quantity_none_for_missing_name(self):
        assert engine.item_match_quantity(_task(target="Abyssal whip"), None) is None
        assert engine.item_match_quantity(_task(target="Abyssal whip"), "") is None
