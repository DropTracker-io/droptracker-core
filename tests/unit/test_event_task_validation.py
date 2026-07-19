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


def test_loot_sweep_unknown_name_still_rejected_without_virtual(_stub_ls):
    # A typo'd name with no aliases and no virtual flag is still an error.
    with pytest.raises(ProblemException) as exc:
        _validate({"type": "loot_sweep", "config": {"groups": [
            {"npcs": ["Kree'arra"], "items": [{"item_name": "Armadyl helmutt"}]}]}})
    assert exc.value.status == 422 and "item" in exc.value.title.lower()
