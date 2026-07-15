"""Unit tests for web_api/task_tiles.py — the resurrected bingo-tile metadata
(pure spec derivation + serialization; no DB)."""

import json

from web_api.task_tiles import (
    COINS_ITEM_ID,
    MAX_TILE_ICONS,
    build_tile,
    icon_asset_path,
    spec_names,
    tile_spec,
)


def _task(**kw):
    base = {"id": 1, "type": "custom", "label": "t", "target": None,
            "target_value": None, "config": None}
    base.update(kw)
    return base


def _tile(task, item_ids=None, npc_ids=None):
    return build_tile(tile_spec(task), item_ids or {}, npc_ids or {})


# ── single-target collection ─────────────────────────────────────────────────

def test_single_item_target():
    tile = _tile(_task(type="item_collection", target="Twisted bow", target_value=3),
                 item_ids={"twisted bow": 20997})
    assert tile["badge"] == "COLLECT"
    assert tile["icons"] == [
        {"type": "item", "id": 20997, "name": "Twisted bow", "quantity": 3}]


def test_single_item_no_quantity_chip_for_one():
    tile = _tile(_task(type="item_collection", target="Twisted bow", target_value=1),
                 item_ids={"twisted bow": 20997})
    assert "quantity" not in tile["icons"][0]


def test_unresolved_item_keeps_none_id():
    tile = _tile(_task(type="item_collection", target="Not A Real Item"))
    assert tile["icons"][0]["id"] is None


# ── list configs ─────────────────────────────────────────────────────────────

def test_any_of_list_with_need():
    cfg = json.dumps({"kind": "any_of", "items": ["Dragon claws", "Elysian sigil"]})
    tile = _tile(_task(type="item_collection", config=cfg, target_value=2),
                 item_ids={"dragon claws": 13652})
    assert tile["badge"] == "ANY 2"
    assert [i["name"] for i in tile["icons"]] == ["Dragon claws", "Elysian sigil"]
    assert tile["icons"][0]["id"] == 13652
    assert tile["icons"][1]["id"] is None


def test_assembly_badge_and_groups_flatten():
    cfg = json.dumps({"kind": "groups", "groups": [
        {"mode": "all_of", "items": ["Godsword shard 1", "Godsword shard 2"]},
        {"mode": "any_of", "need": 1, "items": [{"item_name": "Bandos hilt"}]},
    ]})
    tile = _tile(_task(type="item_collection", config=cfg))
    assert tile["badge"] == "COMBO"
    assert [i["name"] for i in tile["icons"]] == [
        "Godsword shard 1", "Godsword shard 2", "Bandos hilt"]


def test_point_collection_value():
    cfg = json.dumps({"kind": "point_collection", "items": [
        {"item_name": "Zenyte", "points": 5}]})
    tile = _tile(_task(type="item_collection", config=cfg, target_value=1500))
    assert tile["badge"] == "POINTS"
    assert tile["value"] == "1.5K pts"


def test_item_entry_quantity_chip():
    cfg = json.dumps({"kind": "all_of", "items": [
        {"item_name": "Shark", "quantity": 100}, "Manta ray"]})
    tile = _tile(_task(type="item_collection", config=cfg))
    assert tile["badge"] == "ALL ITEMS"
    assert tile["icons"][0]["quantity"] == 100
    assert "quantity" not in tile["icons"][1]


def test_icon_cap_and_overflow():
    cfg = json.dumps({"kind": "any_of", "items": [f"Item {i}" for i in range(20)]})
    tile = _tile(_task(type="item_collection", config=cfg))
    assert len(tile["icons"]) == MAX_TILE_ICONS
    assert tile["icon_overflow"] == 20 - MAX_TILE_ICONS


# ── the other task types ─────────────────────────────────────────────────────

def test_kc_target():
    tile = _tile(_task(type="kc_target", target="Zulrah", target_value=250),
                 npc_ids={"zulrah": 2042})
    assert tile == {"badge": "KC TARGET", "value": "250 KC",
                    "icons": [{"type": "npc", "id": 2042, "name": "Zulrah"}],
                    "icon_overflow": 0}


def test_pb_target_time_format():
    tile = _tile(_task(type="pb_target", target="Vorkath", target_value=90))
    assert tile["badge"] == "KILL TIME"
    assert tile["value"] == "sub 1:30"


def test_xp_target_skill_icon():
    tile = _tile(_task(type="xp_target", target="Slayer", target_value=10_000_000))
    assert tile["value"] == "10.00M XP"
    assert tile["icons"] == [{"type": "skill", "id": None, "name": "slayer"}]


def test_skill_target():
    tile = _tile(_task(type="skill_target", target="Agility", target_value=99))
    assert tile["badge"] == "SKILL LEVEL"
    assert tile["value"] == "Lvl 99"


def test_loot_value_scoped_npcs():
    cfg = json.dumps({"source_npcs": ["Zulrah", "Vorkath"]})
    tile = _tile(_task(type="loot_value", config=cfg, target_value=100_000_000),
                 npc_ids={"zulrah": 2042, "vorkath": 8060})
    assert tile["badge"] == "TOTAL LOOT"
    assert tile["value"] == "100.00M GP"
    assert [i["id"] for i in tile["icons"]] == [2042, 8060]


def test_loot_value_unscoped_uses_coins():
    tile = _tile(_task(type="loot_value", target_value=50_000_000))
    assert tile["icons"] == [{"type": "item", "id": COINS_ITEM_ID, "name": "Coins"}]


def test_custom_task_is_text_only():
    tile = _tile(_task(type="custom", label="Get a funny death"))
    assert tile["badge"] == "CUSTOM"
    assert tile["icons"] == []


def test_ehp_ehb():
    assert _tile(_task(type="ehp_target", target_value=50))["icons"][0]["name"] == "ehp"
    assert _tile(_task(type="ehb_target"))["badge"] == "EHB TARGET"


# ── name collection for bulk resolution ──────────────────────────────────────

def test_spec_names_skips_preresolved_and_normalizes():
    cfg = json.dumps({"source_npcs": ["  Zulrah "]})
    items, npcs = spec_names(tile_spec(_task(type="loot_value", config=cfg)))
    assert items == set()          # coins path not taken; scoped npc only
    assert npcs == {"zulrah"}
    items2, _ = spec_names(tile_spec(_task(type="loot_value")))
    assert items2 == set()         # Coins carries item_id — nothing to resolve


def test_malformed_config_degrades_to_target():
    tile = _tile(_task(type="item_collection", target="Shark", config="{not json"))
    assert tile["badge"] == "COLLECT"
    assert tile["icons"][0]["name"] == "Shark"


# ── icon asset paths (Discord thumbnail resolution) ──────────────────────────

def test_icon_asset_path_item_and_npc():
    assert icon_asset_path({"type": "item", "id": 20997, "name": "Twisted bow"}) == "itemdb/20997.png"
    assert icon_asset_path({"type": "npc", "id": 2042, "name": "Zulrah"}) == "npcdb/2042.png"

def test_icon_asset_path_skill_normalizes_name():
    assert icon_asset_path({"type": "skill", "id": None, "name": "Slayer"}) == "metrics/slayer.png"
    assert icon_asset_path({"type": "skill", "id": None, "name": "Abyssal Sire"}) == "metrics/abyssal_sire.png"

def test_icon_asset_path_none_when_unresolved():
    assert icon_asset_path({"type": "item", "id": None, "name": "Mystery"}) is None
    assert icon_asset_path({"type": "npc", "id": None, "name": "Mystery"}) is None
    assert icon_asset_path({"type": "skill", "id": None, "name": ""}) is None
    assert icon_asset_path({"type": "other", "id": 1}) is None
