"""Unit tests for event-task goal validation (item_collection kinds).

Focus: the ``any_of`` quantity goal and the ``groups`` combined-requirements
kind (all-of + any-of sub-lists, e.g. all godsword shards + any one hilt).
The conftest stubs ``db``; item/NPC canonicalization is monkeypatched to a
fixed known set, so no session is exercised.
"""
from __future__ import annotations

import json

import pytest

import web_api.routes.event_task_validation as etv
from web_api.common import ProblemException

KNOWN_ITEMS = {
    "Boater", "Red boater", "Orange boater",
    "Godsword shard 1", "Godsword shard 2", "Godsword shard 3",
    "Armadyl hilt", "Bandos hilt", "Saradomin hilt", "Zamorak hilt",
    "Justiciar faceguard", "Justiciar chestguard", "Justiciar legguards",
}
_BY_NORM = {n.lower(): n for n in KNOWN_ITEMS}


@pytest.fixture(autouse=True)
def _stub_lookups(monkeypatch):
    monkeypatch.setattr(
        etv, "_canonical_item",
        lambda s, name: _BY_NORM.get((name or "").strip().lower()),
    )


def _validate(body):
    return etv.validate_task_payload(None, body)


def _cfg(payload) -> dict:
    return json.loads(payload["config"])


# ── any_of quantity ──────────────────────────────────────────────────────────

def test_any_of_defaults_to_one():
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "any_of", "items": ["Boater", "Red boater"]},
    })
    assert out["target_value"] == 1


def test_any_of_honors_quantity():
    # "2 boaters": any two qualifying drops from the list complete the task.
    out = _validate({
        "type": "item_collection",
        "target_value": 2,
        "config": {"kind": "any_of", "items": ["Boater", "Red boater", "Orange boater"]},
    })
    assert out["target_value"] == 2
    assert _cfg(out)["kind"] == "any_of"


def test_any_of_rejects_zero_quantity():
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "item_collection",
            "target_value": 0,
            "config": {"kind": "any_of", "items": ["Boater", "Red boater"]},
        })
    assert exc.value.status == 422


# ── groups (combined requirements) ───────────────────────────────────────────

GODSWORD_BODY = {
    "type": "item_collection",
    "config": {
        "kind": "groups",
        "groups": [
            {"mode": "all_of",
             "items": ["Godsword shard 1", "Godsword shard 2", "Godsword shard 3"]},
            {"mode": "any_of",
             "items": ["Armadyl hilt", "Bandos hilt", "Saradomin hilt", "Zamorak hilt"]},
        ],
    },
}


def test_groups_normalizes_and_sums_threshold():
    out = _validate(GODSWORD_BODY)
    cfg = _cfg(out)
    assert cfg["kind"] == "groups"
    assert [g["mode"] for g in cfg["groups"]] == ["all_of", "any_of"]
    assert cfg["groups"][0]["need"] == 3      # all three shards
    assert cfg["groups"][1]["need"] == 1      # any one hilt (default)
    assert out["target_value"] == 4           # sum of group needs
    assert out["target"] is None


def test_groups_any_of_need_carries_through():
    body = json.loads(json.dumps(GODSWORD_BODY))
    body["config"]["groups"][1]["need"] = 2
    out = _validate(body)
    assert _cfg(out)["groups"][1]["need"] == 2
    assert out["target_value"] == 5


def test_groups_reject_unknown_items():
    body = {
        "type": "item_collection",
        "config": {"kind": "groups",
                   "groups": [{"mode": "all_of", "items": ["Nonexistent thing"]}]},
    }
    with pytest.raises(ProblemException) as exc:
        _validate(body)
    assert exc.value.status == 422
    assert "Nonexistent thing" in (exc.value.detail or "")


def test_groups_reject_item_in_two_groups():
    body = {
        "type": "item_collection",
        "config": {"kind": "groups", "groups": [
            {"mode": "all_of", "items": ["Godsword shard 1", "Godsword shard 2"]},
            {"mode": "any_of", "items": ["godsword shard 1", "Armadyl hilt"]},
        ]},
    }
    with pytest.raises(ProblemException) as exc:
        _validate(body)
    assert exc.value.status == 422
    assert "more than one requirement group" in (exc.value.detail or "")


def test_groups_reject_bad_mode_and_empty():
    with pytest.raises(ProblemException):
        _validate({"type": "item_collection",
                   "config": {"kind": "groups", "groups": []}})
    with pytest.raises(ProblemException):
        _validate({"type": "item_collection",
                   "config": {"kind": "groups",
                              "groups": [{"mode": "some_of", "items": ["Boater"]}]}})


def test_groups_reject_too_many_groups():
    body = {
        "type": "item_collection",
        "config": {"kind": "groups", "groups": [
            {"mode": "any_of", "items": ["Boater"]} for _ in range(etv.MAX_CONFIG_GROUPS + 1)
        ]},
    }
    # (Duplicate-item rejection would also fire — group count is checked first.)
    with pytest.raises(ProblemException) as exc:
        _validate(body)
    assert "requirement groups" in (exc.value.detail or "")


# ── any_path (either-or "dryness protection", suggestion #52) ─────────────────

JUSTICIAR_BODY = {
    "type": "item_collection",
    "config": {
        "kind": "any_path",
        "paths": [
            {"label": "Full set",
             "groups": [{"mode": "all_of",
                         "items": ["Justiciar faceguard", "Justiciar chestguard",
                                   "Justiciar legguards"]}]},
            {"label": "Any 5 pieces",
             "groups": [{"mode": "any_of", "need": 5,
                         "items": ["Justiciar faceguard", "Justiciar chestguard",
                                   "Justiciar legguards"]}]},
        ],
    },
}


def test_any_path_normalizes_and_pins_percentage_threshold():
    out = _validate(json.loads(json.dumps(JUSTICIAR_BODY)))
    cfg = _cfg(out)
    assert cfg["kind"] == "any_path"
    assert [p["label"] for p in cfg["paths"]] == ["Full set", "Any 5 pieces"]
    assert cfg["paths"][0]["groups"][0]["need"] == 3
    assert cfg["paths"][1]["groups"][0]["need"] == 5
    assert out["target_value"] == etv.ANY_PATH_THRESHOLD
    assert out["target"] is None


def test_any_path_allows_items_to_repeat_across_paths():
    # The same drop advancing every path is the whole point — only
    # within-path duplicates are rejected.
    out = _validate(json.loads(json.dumps(JUSTICIAR_BODY)))
    assert len(_cfg(out)["paths"]) == 2


def test_any_path_rejects_within_path_duplicates():
    with pytest.raises(ProblemException):
        _validate({
            "type": "item_collection",
            "config": {"kind": "any_path", "paths": [
                {"groups": [{"mode": "all_of", "items": ["Boater"]},
                            {"mode": "any_of", "items": ["Boater"]}]},
                {"groups": [{"mode": "any_of", "items": ["Red boater"]}]},
            ]},
        })


def test_any_path_requires_two_paths():
    for paths in ([], [{"groups": [{"mode": "any_of", "items": ["Boater"]}]}]):
        with pytest.raises(ProblemException) as exc:
            _validate({"type": "item_collection",
                       "config": {"kind": "any_path", "paths": paths}})
        assert "at least two paths" in (exc.value.detail or "")


def test_any_path_rejects_too_many_paths():
    body = {
        "type": "item_collection",
        "config": {"kind": "any_path", "paths": [
            {"groups": [{"mode": "any_of", "items": ["Boater"]}]}
            for _ in range(etv.MAX_CONFIG_PATHS + 1)
        ]},
    }
    with pytest.raises(ProblemException) as exc:
        _validate(body)
    assert "paths per task" in (exc.value.detail or "")


def test_any_path_rejects_unknown_items():
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "item_collection",
            "config": {"kind": "any_path", "paths": [
                {"groups": [{"mode": "any_of", "items": ["Nonexistent thing"]}]},
                {"groups": [{"mode": "any_of", "items": ["Boater"]}]},
            ]},
        })
    assert exc.value.status == 422


# ── any_path metric paths ("boss pet OR 5,000 KC / GP goals") ─────────────────

PATH_NPCS = {"Kree'arra", "General Graardor", "Zulrah"}
_PATH_NPC_BY_NORM = {n.lower(): n for n in PATH_NPCS}


@pytest.fixture
def _stub_path_npcs(monkeypatch):
    monkeypatch.setattr(
        etv, "_canonical_npc",
        lambda s, name: _PATH_NPC_BY_NORM.get((name or "").strip().lower()),
    )


def test_metric_paths_normalize(_stub_path_npcs):
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "any_path", "paths": [
            {"label": "Any hilt",
             "groups": [{"mode": "any_of", "need": 1, "items": ["Armadyl hilt"]}]},
            {"label": "Grind it", "metric": "kc",
             "npcs": ["kree'arra", "GENERAL GRAARDOR", "Kree'arra"], "need": 5000},
            {"metric": "loot_value", "npcs": ["zulrah"], "need": 10_000_000},
        ]},
    })
    cfg = _cfg(out)
    assert out["target_value"] == etv.ANY_PATH_THRESHOLD
    kc = cfg["paths"][1]
    assert kc == {"metric": "kc", "need": 5000, "label": "Grind it",
                  "npcs": ["Kree'arra", "General Graardor"]}
    gp = cfg["paths"][2]
    assert gp == {"metric": "loot_value", "need": 10_000_000, "npcs": ["Zulrah"]}


def test_metric_only_or_task_is_valid(_stub_path_npcs):
    # "5,000 KC OR 10M GP" with no item path at all.
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "any_path", "paths": [
            {"metric": "kc", "npcs": ["Kree'arra"], "need": 5000},
            {"metric": "loot_value", "need": 10_000_000},
        ]},
    })
    cfg = _cfg(out)
    assert [p["metric"] for p in cfg["paths"]] == ["kc", "loot_value"]
    assert "npcs" not in cfg["paths"][1]  # unscoped GP path stays unscoped


def test_kc_path_requires_npcs_and_need(_stub_path_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection",
                   "config": {"kind": "any_path", "paths": [
                       {"metric": "kc", "need": 100},
                       {"metric": "loot_value", "need": 1},
                   ]}})
    assert "at least one NPC" in (exc.value.detail or "")
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection",
                   "config": {"kind": "any_path", "paths": [
                       {"metric": "kc", "npcs": ["Kree'arra"]},
                       {"metric": "loot_value", "need": 1},
                   ]}})
    assert exc.value.status == 422


def test_metric_path_rejects_unknown_npc_and_metric(_stub_path_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection",
                   "config": {"kind": "any_path", "paths": [
                       {"metric": "kc", "npcs": ["Notreal the Fake"], "need": 5},
                       {"metric": "loot_value", "need": 1},
                   ]}})
    assert "Notreal the Fake" in (exc.value.detail or "")
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection",
                   "config": {"kind": "any_path", "paths": [
                       {"metric": "xp", "need": 5},
                       {"metric": "loot_value", "need": 1},
                   ]}})
    assert "metric" in (exc.value.detail or "")


def test_kc_path_rejects_oversized_npc_list(_stub_path_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection",
                   "config": {"kind": "any_path", "paths": [
                       {"metric": "kc", "need": 5,
                        "npcs": ["Zulrah"] * (etv.MAX_KC_NPCS + 1)},
                       {"metric": "loot_value", "need": 1},
                   ]}})
    assert exc.value.status == 422


# ── any_path points paths ("Full set OR 500 pts of listed items") ─────────────

def test_points_path_normalizes_weights_and_goal():
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "any_path", "paths": [
            {"label": "Full set",
             "groups": [{"mode": "all_of",
                         "items": ["Justiciar faceguard", "Justiciar chestguard",
                                   "Justiciar legguards"]}]},
            {"label": "500 points", "kind": "points", "need": 500,
             "items": [{"item_name": "Armadyl hilt", "points": 50},
                       {"item_name": "Bandos hilt", "points": 3.6}]},
        ]},
    })
    cfg = _cfg(out)
    pts = cfg["paths"][1]
    assert pts["kind"] == "points" and pts["need"] == 500
    assert pts["label"] == "500 points"
    # Whole-number weights (3.6 rounds to 4), each entry canonicalized.
    assert pts["items"] == [
        {"item_name": "Armadyl hilt", "points": 50},
        {"item_name": "Bandos hilt", "points": 4},
    ]
    assert out["target_value"] == etv.ANY_PATH_THRESHOLD
    assert out["target"] is None


def test_points_path_defaults_weight_to_one():
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "any_path", "paths": [
            {"groups": [{"mode": "any_of", "items": ["Boater"]}]},
            {"kind": "points", "need": 10, "items": ["Red boater", "Orange boater"]},
        ]},
    })
    assert _cfg(out)["paths"][1]["items"] == [
        {"item_name": "Red boater", "points": 1},
        {"item_name": "Orange boater", "points": 1},
    ]


def test_points_path_requires_goal_and_items():
    for path in ({"kind": "points", "items": [{"item_name": "Boater", "points": 2}]},
                 {"kind": "points", "need": 5, "items": []}):
        with pytest.raises(ProblemException) as exc:
            _validate({"type": "item_collection",
                       "config": {"kind": "any_path", "paths": [
                           path, {"groups": [{"mode": "any_of", "items": ["Boater"]}]},
                       ]}})
        assert exc.value.status == 422


def test_points_path_rejects_unknown_items():
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection",
                   "config": {"kind": "any_path", "paths": [
                       {"kind": "points", "need": 5,
                        "items": [{"item_name": "Nonexistent thing", "points": 1}]},
                       {"groups": [{"mode": "any_of", "items": ["Boater"]}]},
                   ]}})
    assert exc.value.status == 422


# ── pet_collection ────────────────────────────────────────────────────────────
# Pet names resolve against the real utils.osrs_pets taxonomy (a pure leaf
# module — not stubbed), so these use genuine in-game pet names.

def test_pet_specific_canonicalizes_and_defaults_quantity():
    out = _validate({"type": "pet_collection", "target": "baby mole"})
    assert out["target"] == "Baby mole"
    assert out["target_value"] == 1
    assert out["config"] is None


def test_pet_specific_unknown_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "pet_collection", "target": "Not a pet"})
    assert exc.value.status == 422


def test_pet_any_has_no_target_or_config():
    out = _validate({"type": "pet_collection"})
    assert out["target"] is None
    assert out["config"] is None
    assert out["target_value"] == 1


def test_pet_any_honors_count():
    out = _validate({"type": "pet_collection", "target_value": 5})
    assert out["target_value"] == 5


def test_pet_category_normalized():
    out = _validate({
        "type": "pet_collection",
        "config": {"categories": ["boss", "boss", "skilling"]},
    })
    assert _cfg(out) == {"categories": ["boss", "skilling"]}
    assert out["target"] is None


def test_pet_category_unknown_key_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "pet_collection", "config": {"categories": ["dinosaur"]}})
    assert exc.value.status == 422


def test_pet_category_empty_list_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "pet_collection", "config": {"categories": []}})
    assert exc.value.status == 422


def test_pet_list_canonicalizes_dedupes_and_sorts():
    # Customized category preset: explicit allow list of pet names.
    out = _validate({
        "type": "pet_collection",
        "config": {"pets": ["beaver", "Baby mole", "BEAVER"]},
    })
    assert _cfg(out) == {"pets": ["Baby mole", "Beaver"]}
    assert out["target"] is None
    assert out["target_value"] == 1


def test_pet_list_allows_misc_pets():
    # Listing a misc pet is deliberate — allowed, like a specific-pet target.
    out = _validate({
        "type": "pet_collection",
        "config": {"pets": ["Chompy chick", "Vorki"]},
    })
    assert _cfg(out) == {"pets": ["Chompy chick", "Vorki"]}


def test_pet_list_unknown_pet_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "pet_collection", "config": {"pets": ["Baby mole", "Dinosaur"]}})
    assert exc.value.status == 422


def test_pet_list_empty_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "pet_collection", "config": {"pets": []}})
    assert exc.value.status == 422


def test_pet_list_with_categories_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "pet_collection",
            "config": {"pets": ["Baby mole"], "categories": ["boss"]},
        })
    assert exc.value.status == 422


# ── kc_target (single + multi-NPC via config.npcs) ────────────────────────────

KC_NPCS = {"Dagannoth Rex", "Dagannoth Prime", "Dagannoth Supreme", "Zulrah"}
_KC_BY_NORM = {n.lower(): n for n in KC_NPCS}


@pytest.fixture
def _stub_kc_npcs(monkeypatch):
    monkeypatch.setattr(
        etv, "_canonical_npc",
        lambda s, name: _KC_BY_NORM.get((name or "").strip().lower()),
    )


def test_kc_single_target_stays_config_free(_stub_kc_npcs):
    out = _validate({"type": "kc_target", "target": "zulrah", "target_value": 50})
    assert out == {"target": "Zulrah", "target_value": 50, "config": None}


def test_kc_multi_npc_normalizes(_stub_kc_npcs):
    out = _validate({
        "type": "kc_target", "target_value": 50,
        "config": {"npcs": ["dagannoth rex", "Dagannoth Prime",
                            "DAGANNOTH SUPREME", "Dagannoth Rex"]},  # dupe folds
    })
    assert out["target"] == "Dagannoth Rex"  # first NPC doubles as the target
    assert out["target_value"] == 50
    assert _cfg(out) == {"npcs": ["Dagannoth Rex", "Dagannoth Prime",
                                  "Dagannoth Supreme"]}


def test_kc_single_entry_list_collapses_to_target(_stub_kc_npcs):
    out = _validate({"type": "kc_target", "target_value": 10,
                     "config": {"npcs": ["zulrah"]}})
    assert out == {"target": "Zulrah", "target_value": 10, "config": None}


def test_kc_unknown_npc_in_list_rejected(_stub_kc_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "kc_target", "target_value": 10,
                   "config": {"npcs": ["Zulrah", "Notreal the Fake"]}})
    assert exc.value.status == 422
    assert "Notreal the Fake" in exc.value.detail


def test_kc_empty_or_oversized_list_rejected(_stub_kc_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "kc_target", "target_value": 10, "config": {"npcs": []}})
    assert exc.value.status == 422
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "kc_target", "target_value": 10,
                   "config": {"npcs": ["Zulrah"] * (etv.MAX_KC_NPCS + 1)}})
    assert exc.value.status == 422


def test_pb_target_ignores_npcs_config(_stub_kc_npcs):
    # pb_target stays single-NPC: a stray npcs list must not leak into config.
    out = _validate({"type": "pb_target", "target": "Zulrah", "target_value": 70,
                     "config": {"npcs": ["Dagannoth Rex"]}})
    assert out == {"target": "Zulrah", "target_value": 70, "config": None}


# ── pb completion requirements (times / unique_players / whole_team) ─────────

def test_pb_times_mode_normalizes(_stub_kc_npcs):
    out = _validate({"type": "pb_target", "target": "Zulrah", "target_value": 70,
                     "config": {"mode": "times", "need": 5}})
    assert json.loads(out["config"]) == {"mode": "times", "need": 5}
    # ×1 collapses to the legacy config-free single-shot.
    out = _validate({"type": "pb_target", "target": "Zulrah", "target_value": 70,
                     "config": {"mode": "times", "need": 1}})
    assert out["config"] is None


def test_pb_unique_players_mode_kept_even_at_one(_stub_kc_npcs):
    out = _validate({"type": "pb_target", "target": "Zulrah", "target_value": 70,
                     "config": {"mode": "unique_players", "need": 1}})
    assert json.loads(out["config"]) == {"mode": "unique_players", "need": 1}


def test_pb_whole_team_strips_need(_stub_kc_npcs):
    out = _validate({"type": "pb_target", "target": "Zulrah", "target_value": 70,
                     "config": {"mode": "whole_team", "need": 99}})
    assert json.loads(out["config"]) == {"mode": "whole_team"}


def test_pb_bad_mode_or_need_rejected(_stub_kc_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "pb_target", "target": "Zulrah", "target_value": 70,
                   "config": {"mode": "nonsense"}})
    assert "PB completion mode" in (exc.value.detail or "")
    for bad_need in (0, "five", etv.MAX_PB_NEED + 1):
        with pytest.raises(ProblemException):
            _validate({"type": "pb_target", "target": "Zulrah", "target_value": 70,
                       "config": {"mode": "times", "need": bad_need}})


# ── loot_sweep v2 (nested groups + NPC scoping + batched decay) ───────────────

KNOWN_NPCS = {"Kree'arra", "Ahrim the Blighted", "Dharok the Wretched",
              "Alchemical Hydra", "Vet'ion", "Calvar'ion"}
_NPC_BY_NORM = {n.lower(): n for n in KNOWN_NPCS}
KNOWN_LS_ITEMS = {
    "Armadyl helmet": 11826, "Armadyl chestplate": 11828, "Armadyl hilt": 11810,
    "Pet kree'arra": 22473, "Ahrim's hood": 4708, "Ahrim's staff": 4710,
    "Brimstone key": 22975, "Dragon 2h sword": 7158,
}
_LS_BY_NORM = {n.lower(): (n, i) for n, i in KNOWN_LS_ITEMS.items()}


@pytest.fixture
def _stub_ls(monkeypatch):
    monkeypatch.setattr(etv, "_canonical_item_with_id",
                        lambda s, name: _LS_BY_NORM.get((name or "").strip().lower(), (None, None)))
    monkeypatch.setattr(etv, "_canonical_npc",
                        lambda s, name: _NPC_BY_NORM.get((name or "").strip().lower()))


def test_loot_sweep_valid_group(_stub_ls):
    out = _validate({"type": "loot_sweep", "config": {
        "decay_percent": 20, "set_bonus_points": 0,
        "groups": [{"label": "Kree'arra", "npcs": ["Kree'arra"], "bonus_points": 40,
                    "items": [{"item_name": "Armadyl helmet", "points": 9},
                              {"item_name": "Pet kree'arra", "points": 60,
                               "counts_for_group": False}]}]}})
    cfg = _cfg(out)
    assert cfg["kind"] == "loot_sweep"
    g = cfg["groups"][0]
    assert g["npcs"] == ["Kree'arra"] and g["bonus_points"] == 40
    assert g["items"][0]["item_id"] == 11826
    assert g["items"][1]["counts_for_group"] is False
    assert out["target"] is None and out["target_value"] is None


def test_loot_sweep_unknown_npc_rejected(_stub_ls):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_sweep", "config": {"groups": [
            {"npcs": ["Nonexistent Boss"], "items": [{"item_name": "Armadyl helmet"}]}]}})
    assert exc.value.status == 422 and "NPC" in exc.value.title


def test_loot_sweep_unknown_item_rejected(_stub_ls):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_sweep", "config": {"groups": [
            {"npcs": ["Kree'arra"], "items": [{"item_name": "Nonexistent item"}]}]}})
    assert exc.value.status == 422 and "item" in exc.value.title.lower()


def test_loot_sweep_item_in_two_groups_rejected(_stub_ls):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_sweep", "config": {"groups": [
            {"npcs": ["Vet'ion"], "items": [{"item_name": "Dragon 2h sword"}]},
            {"npcs": ["Calvar'ion"], "items": [{"item_name": "Dragon 2h sword"}]}]}})
    assert exc.value.status == 422


def test_loot_sweep_awards_per_tier(_stub_ls):
    out = _validate({"type": "loot_sweep", "config": {"groups": [
        {"npcs": ["Alchemical Hydra"], "items": [
            {"item_name": "Brimstone key", "points": 4, "awards_per_tier": 3}]}]}})
    it = _cfg(out)["groups"][0]["items"][0]
    assert it["awards_per_tier"] == 3
    # max_awards is left implicit (the scorer defaults it to 5 tiers * apt = 15).
    assert "max_awards" not in it


def test_loot_sweep_multi_npc_group(_stub_ls):
    out = _validate({"type": "loot_sweep", "config": {"groups": [
        {"label": "Vet'ion", "npcs": ["Vet'ion", "Calvar'ion"],
         "items": [{"item_name": "Dragon 2h sword", "points": 8}]}]}})
    assert _cfg(out)["groups"][0]["npcs"] == ["Vet'ion", "Calvar'ion"]


def test_loot_sweep_v1_backcompat_flat_items(_stub_ls):
    out = _validate({"type": "loot_sweep", "config": {
        "set_bonus_points": 40, "npcs": ["Kree'arra"],
        "items": [{"item_name": "Armadyl helmet", "points": 9}]}})
    cfg = _cfg(out)
    assert len(cfg["groups"]) == 1
    assert cfg["groups"][0]["bonus_points"] == 40


def test_loot_sweep_pet_source(_stub_ls):
    out = _validate({"type": "loot_sweep", "config": {"groups": [
        {"label": "Kree'arra", "npcs": ["Kree'arra"], "bonus_points": 40, "items": [
            {"item_name": "Armadyl helmet", "points": 9},
            {"item_name": "Nexling", "points": 40, "source": "pet", "counts_for_group": False}]}]}})
    items = _cfg(out)["groups"][0]["items"]
    pet = [i for i in items if i.get("source") == "pet"][0]
    assert pet["item_name"] == "Nexling" and pet["counts_for_group"] is False


def test_loot_sweep_unknown_pet_rejected(_stub_ls):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_sweep", "config": {"groups": [
            {"npcs": ["Kree'arra"], "items": [
                {"item_name": "Not a pet", "source": "pet"}]}]}})
    assert exc.value.status == 422


def test_loot_sweep_group_image_url_relative(_stub_ls):
    out = _validate({"type": "loot_sweep", "config": {"groups": [
        {"npcs": ["Kree'arra"], "image_url": "/img/x/kreearra.png",
         "items": [{"item_name": "Armadyl helmet"}]}]}})
    assert _cfg(out)["groups"][0]["image_url"] == "/img/x/kreearra.png"


def test_loot_sweep_group_rejects_external_image(_stub_ls):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_sweep", "config": {"groups": [
            {"npcs": ["Kree'arra"], "image_url": "https://evil.example/x.png",
             "items": [{"item_name": "Armadyl helmet"}]}]}})
    assert exc.value.status == 422


def test_loot_sweep_virtual_label_backed_by_pieces(_stub_ls):
    out = _validate({"type": "loot_sweep", "config": {"groups": [
        {"npcs": ["Kree'arra"], "bonus_points": 10,
         "items": [{"item_name": "Any armadyl piece", "virtual": True, "required": 2,
                    "match_names": ["Armadyl helmet", "Armadyl chestplate"]}]}]}})
    it = _cfg(out)["groups"][0]["items"][0]
    assert it["virtual"] is True and it["required"] == 2
    assert "item_id" not in it  # the label is not a real item
    assert it["match_names"] == ["Armadyl helmet", "Armadyl chestplate"]


def test_loot_sweep_virtual_without_pieces_rejected(_stub_ls):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_sweep", "config": {"groups": [
            {"npcs": ["Kree'arra"],
             "items": [{"item_name": "Any armadyl piece", "virtual": True}]}]}})
    assert exc.value.status == 422


def test_loot_sweep_same_group_name_reuse_allowed(_stub_ls):
    # An ancestral piece as its own entry AND inside an "any" pool in the SAME
    # group is allowed (additive scoring); no 422.
    out = _validate({"type": "loot_sweep", "config": {"groups": [
        {"npcs": ["Kree'arra"], "bonus_points": 10, "items": [
            {"item_name": "Armadyl helmet", "points": 9},
            {"item_name": "Any armadyl piece", "virtual": True, "required": 2,
             "match_names": ["Armadyl helmet", "Armadyl chestplate"]},
        ]}]}})
    items = _cfg(out)["groups"][0]["items"]
    assert items[0]["item_name"] == "Armadyl helmet"
    assert items[1]["match_names"] == ["Armadyl helmet", "Armadyl chestplate"]


def test_loot_sweep_unknown_name_still_rejected_without_virtual(_stub_ls):
    # A typo'd name with no aliases and no virtual flag is still an error.
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_sweep", "config": {"groups": [
            {"npcs": ["Kree'arra"], "items": [{"item_name": "Armadyl helmutt"}]}]}})
    assert exc.value.status == 422 and "item" in exc.value.title.lower()


# ── item_collection source-NPC restriction (source_npcs / item_npcs) ─────────
# Opt-in: single-item tasks carry config.source_npcs; multi-item tasks carry a
# flat config.item_npcs map (so it reaches groups/any_path, whose stored item
# lists are bare name strings).

SOURCE_NPCS = {"Zulrah", "Vet'ion", "Callisto", "Chaos Fanatic"}
_SRC_NPC_BY_NORM = {n.lower(): n for n in SOURCE_NPCS}


@pytest.fixture
def _stub_src_npcs(monkeypatch):
    monkeypatch.setattr(
        etv, "_canonical_npc",
        lambda s, name: _SRC_NPC_BY_NORM.get((name or "").strip().lower()),
    )


def test_single_item_unrestricted_has_no_config():
    out = _validate({"type": "item_collection", "target": "Boater", "target_value": 1})
    assert out["target"] == "Boater"
    assert out["target_value"] == 1
    assert out["config"] is None


def test_single_item_source_npcs_normalizes(_stub_src_npcs):
    out = _validate({
        "type": "item_collection", "target": "armadyl hilt",   # canonicalizes
        "config": {"source_npcs": ["zulrah", "Vet'ion", "zulrah"]},  # dupe folds
    })
    assert out["target"] == "Armadyl hilt"
    assert out["target_value"] == 1
    assert _cfg(out) == {"source_npcs": ["Zulrah", "Vet'ion"]}


def test_single_item_empty_source_npcs_is_unrestricted(_stub_src_npcs):
    out = _validate({
        "type": "item_collection", "target": "Boater",
        "config": {"source_npcs": []},
    })
    assert out["config"] is None


def test_single_item_unknown_source_npc_rejected(_stub_src_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection", "target": "Boater",
                   "config": {"source_npcs": ["Zulrah", "Notreal Boss"]}})
    assert exc.value.status == 422
    assert "Notreal Boss" in (exc.value.detail or "")


def test_single_item_too_many_source_npcs_rejected(_stub_src_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection", "target": "Boater",
                   "config": {"source_npcs": ["Zulrah"] * (etv.MAX_SOURCE_NPCS + 1)}})
    assert exc.value.status == 422


def test_source_npc_cap_clears_the_item_source_list_limit():
    """The picker seeds a restriction with EVERY wiki drop source of the item
    and the configurator prunes from there, so the save-side cap must admit
    the largest list the picker can hand back — otherwise seeding a
    high-source item (Uncut diamond: ~453 sources) 422s before the user
    touches anything."""
    from db.item_sources import SOURCES_LIMIT
    assert etv.MAX_SOURCE_NPCS >= SOURCES_LIMIT


def test_any_of_item_npcs_attached(_stub_src_npcs):
    out = _validate({
        "type": "item_collection", "target_value": 1,
        "config": {"kind": "any_of", "items": ["Boater", "Red boater"],
                   "item_npcs": {"Boater": ["Zulrah", "Vet'ion"]}},
    })
    cfg = _cfg(out)
    assert cfg["kind"] == "any_of"
    assert cfg["item_npcs"] == {"Boater": ["Zulrah", "Vet'ion"]}


def test_item_npcs_canonicalizes_key_and_values(_stub_src_npcs):
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "all_of", "items": ["Boater", "Red boater"],
                   "item_npcs": {"boater": ["zulrah"]}},
    })
    assert _cfg(out)["item_npcs"] == {"Boater": ["Zulrah"]}


def test_item_npcs_for_item_not_in_list_dropped(_stub_src_npcs):
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "all_of", "items": ["Boater"],
                   "item_npcs": {"Red boater": ["Zulrah"]}},  # not in the list
    })
    assert "item_npcs" not in _cfg(out)


def test_item_npcs_empty_value_dropped(_stub_src_npcs):
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "all_of", "items": ["Boater"],
                   "item_npcs": {"Boater": []}},  # unrestricted item
    })
    assert "item_npcs" not in _cfg(out)


def test_item_npcs_unknown_item_rejected(_stub_src_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection",
                   "config": {"kind": "all_of", "items": ["Boater"],
                              "item_npcs": {"Nonexistent thing": ["Zulrah"]}}})
    assert exc.value.status == 422


def test_item_npcs_unknown_npc_rejected(_stub_src_npcs):
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "item_collection",
                   "config": {"kind": "all_of", "items": ["Boater"],
                              "item_npcs": {"Boater": ["Notreal Boss"]}}})
    assert exc.value.status == 422


def test_groups_item_npcs_attached(_stub_src_npcs):
    # Proves the flat item_npcs map reaches the groups kind (its stored item
    # lists are bare name strings, so per-entry npcs wouldn't survive).
    body = json.loads(json.dumps(GODSWORD_BODY))
    body["config"]["item_npcs"] = {"Godsword shard 1": ["Vet'ion"]}
    out = _validate(body)
    cfg = _cfg(out)
    assert cfg["kind"] == "groups"
    assert cfg["item_npcs"] == {"Godsword shard 1": ["Vet'ion"]}


def test_any_path_item_npcs_attached(_stub_src_npcs):
    body = json.loads(json.dumps(JUSTICIAR_BODY))
    body["config"]["item_npcs"] = {"Justiciar faceguard": ["Zulrah"]}
    out = _validate(body)
    cfg = _cfg(out)
    assert cfg["kind"] == "any_path"
    assert cfg["item_npcs"] == {"Justiciar faceguard": ["Zulrah"]}


def test_kind_less_config_with_items_still_rejected(_stub_src_npcs):
    # Relaxing the kind gate for source_npcs must NOT let a list task that
    # forgot its 'kind' silently collapse to a single-item task.
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "item_collection", "target": "Boater", "target_value": 1,
            "config": {"items": ["Boater", "Red boater"]},  # no kind
        })
    assert exc.value.status == 422


# ── NPC source aliases ("Wintertodt" → its reward containers) ─────────────────
# Display aliases are accepted on write and expanded to the real recorded
# source names, so configs only ever hold names the engine can match drops by.

ALIAS_NPCS = {"Reward cart (Wintertodt)", "Supply crate (Wintertodt)", "Zulrah"}
_ALIAS_NPC_BY_NORM = {n.lower(): n for n in ALIAS_NPCS}


@pytest.fixture
def _stub_alias_npcs(monkeypatch):
    monkeypatch.setattr(
        etv, "_canonical_npc",
        lambda s, name: _ALIAS_NPC_BY_NORM.get((name or "").strip().lower()),
    )


def test_source_npcs_alias_expands_to_members(_stub_alias_npcs):
    out = _validate({
        "type": "item_collection", "target": "Boater",
        "config": {"source_npcs": ["Wintertodt"]},
    })
    assert _cfg(out) == {"source_npcs": ["Reward cart (Wintertodt)",
                                         "Supply crate (Wintertodt)"]}


def test_source_npcs_alias_dedupes_against_member(_stub_alias_npcs):
    out = _validate({
        "type": "item_collection", "target": "Boater",
        "config": {"source_npcs": ["Supply crate (Wintertodt)", "Wintertodt"]},
    })
    assert sorted(_cfg(out)["source_npcs"]) == [
        "Reward cart (Wintertodt)", "Supply crate (Wintertodt)"]


def test_kc_target_alias_expands_to_multi_npc(_stub_alias_npcs):
    out = _validate({"type": "kc_target", "target": "Wintertodt", "target_value": 50})
    assert out["target"] == "Reward cart (Wintertodt)"
    assert _cfg(out) == {"npcs": ["Reward cart (Wintertodt)",
                                  "Supply crate (Wintertodt)"]}


def test_loot_value_alias_expands(_stub_alias_npcs):
    out = _validate({"type": "loot_value", "target_value": 1_000_000,
                     "config": {"source_npcs": ["Wintertodt"]}})
    assert _cfg(out) == {"source_npcs": ["Reward cart (Wintertodt)",
                                         "Supply crate (Wintertodt)"]}


def test_pb_target_does_not_expand_alias(_stub_alias_npcs):
    # pb_target is single-NPC; the alias isn't a real NPC there → 422.
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "pb_target", "target": "Wintertodt", "target_value": 70})
    assert exc.value.status == 422


# ── pets mixed into item lists (config.pet_items) ─────────────────────────────
# Names flagged as pets validate against the pet taxonomy (not the item DB),
# canonicalize, and are excluded from the item_npcs source map.

def test_pet_items_canonicalize_and_persist():
    out = _validate({
        "type": "item_collection", "target_value": 1,
        "config": {"kind": "any_of", "items": ["Boater", "baby mole"],
                   "pet_items": ["baby mole"]},
    })
    cfg = _cfg(out)
    assert [i["item_name"] for i in cfg["items"]] == ["Boater", "Baby mole"]
    assert cfg["pet_items"] == ["Baby mole"]


def test_pet_items_unknown_pet_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "item_collection", "target_value": 1,
            "config": {"kind": "any_of", "items": ["Boater", "Fakepet"],
                       "pet_items": ["Fakepet"]},
        })
    assert exc.value.status == 422
    assert "pet" in exc.value.title.lower()


def test_pet_items_stale_flag_ignored():
    # A pet_items name not actually in the list is dropped, not an error.
    out = _validate({
        "type": "item_collection", "target_value": 1,
        "config": {"kind": "any_of", "items": ["Boater", "Red boater"],
                   "pet_items": ["Baby mole"]},
    })
    assert "pet_items" not in _cfg(out)


def test_pet_items_malformed_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "item_collection", "target_value": 1,
            "config": {"kind": "any_of", "items": ["Boater", "Red boater"],
                       "pet_items": "Baby mole"},
        })
    assert exc.value.status == 422


def test_pet_items_excluded_from_item_npcs(_stub_src_npcs):
    # A source restriction on a pet-flagged name is silently dropped — pets
    # have no drop source.
    out = _validate({
        "type": "item_collection", "target_value": 2,
        "config": {"kind": "all_of", "items": ["Boater", "Baby mole"],
                   "pet_items": ["Baby mole"],
                   "item_npcs": {"Boater": ["Zulrah"]}},
    })
    cfg = _cfg(out)
    assert cfg["item_npcs"] == {"Boater": ["Zulrah"]}
    assert cfg["pet_items"] == ["Baby mole"]


def test_pet_items_in_groups_and_paths():
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "groups",
                   "groups": [{"mode": "all_of", "items": ["Boater"]},
                              {"mode": "any_of", "items": ["Baby mole"]}],
                   "pet_items": ["Baby mole"]},
    })
    cfg = _cfg(out)
    assert cfg["groups"][1]["items"] == ["Baby mole"]
    assert cfg["pet_items"] == ["Baby mole"]


# ── point_collection weights are whole numbers ────────────────────────────────

def test_point_collection_weights_round_to_int():
    out = _validate({
        "type": "item_collection", "target_value": 100,
        "config": {"kind": "point_collection",
                   "items": [{"item_name": "Boater", "points": 2.4},
                             {"item_name": "Red boater", "points": 0.2},
                             {"item_name": "Orange boater", "points": 50}]},
    })
    pts = {i["item_name"]: i["points"] for i in _cfg(out)["items"]}
    assert pts == {"Boater": 2, "Red boater": 1, "Orange boater": 50}
    assert all(isinstance(p, int) for p in pts.values())


def test_config_too_large_rejected(monkeypatch):
    # 100 items each restricted to 100 long NPC names serializes well past the
    # config column's ~64KB ceiling -> 422 rather than a 500 on save.
    monkeypatch.setattr(etv, "_canonical_item", lambda s, n: (n or "").strip() or None)
    monkeypatch.setattr(etv, "_canonical_npc", lambda s, n: (n or "").strip() or None)
    items = [f"Item number {i:04d}" for i in range(100)]
    npcs = [f"Boss with a fairly long name number {j:04d}" for j in range(100)]
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "item_collection",
            "config": {"kind": "all_of", "items": items,
                       "item_npcs": {it: npcs for it in items}},
        })
    assert exc.value.status == 422
    assert "too large" in (exc.value.title or "").lower()


# ── goal magnitude (web69a) ──────────────────────────────────────────────────
# target_value is BIGINT, so multi-billion goals must validate rather than
# reach MySQL as an out-of-range INT (error 1264 -> a 500 on save).

def test_loot_value_accepts_multi_billion_gp():
    # The reported failure: "Obtain 5b in Value from All Drops", no source NPC.
    out = _validate({"type": "loot_value", "target_value": 5_000_000_000})
    assert out["target_value"] == 5_000_000_000
    assert out["config"] is None  # no NPC restriction = any drop counts


def test_any_path_loot_value_leg_accepts_multi_billion_gp(_stub_path_npcs):
    # The same GP goal reached through an any_path metric leg.
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "any_path", "paths": [
            {"metric": "kc", "npcs": ["Kree'arra"], "need": 5000},
            {"metric": "loot_value", "need": 3_000_000_000},
        ]},
    })
    gp = [p for p in _cfg(out)["paths"] if p.get("metric") == "loot_value"][0]
    assert gp["need"] == 3_000_000_000


def test_target_value_past_js_safe_integer_rejected():
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_value", "target_value": etv.MAX_TARGET_VALUE + 1})
    assert exc.value.status == 422
    assert "target value" in (exc.value.title or "").lower()


def test_bounded_goal_keeps_its_own_ceiling():
    # A goal with a tighter bound is unaffected by MAX_TARGET_VALUE.
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "skill_target", "target": "Attack", "target_value": 120})
    assert exc.value.status == 422
