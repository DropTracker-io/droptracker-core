"""api/panel_cards.py: the Groups/Players page cards the plugin draws.

The wording is pinned to what the plugin's own pages showed before the cards
moved here (verified pixel-for-pixel against plugin 6.1.12's renders), and the
actions to the closed set the plugin implements.
"""
from __future__ import annotations

import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "api", "panel_cards.py")
_spec = importlib.util.spec_from_file_location("_panel_cards_real", _PATH)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

ACTIONS = {"player", "web", "discord", "lootboard", "refresh"}


def _all_actions(card):
    found = [s["action"] for s in card["stats"] if s["action"]]
    return found + [b["action"] for b in card["actions"]]


def test_group_card():
    card = pc.group_card(name="Test Clan", group_id=7, description="Hi", icon_path="clans/7/icon.png",
                         members=123, rank="4/269", loot="1.25B", top_player="Ra ine", recent=[{"x": 1}],
                         invite_code="abc123")
    assert [(s["label"], s["value"]) for s in card["stats"]] == [
        ("Members", "123"), ("Global Rank", "#4/269"), ("Monthly Loot", "1.25B GP"), ("Top Player", "Ra ine")]
    assert card["stats"][3]["action"] == {"type": "player", "value": "Ra ine"}
    assert [b["label"] for b in card["actions"]] == ["Discord", "View Lootboard"]
    assert card["actions"][1] == {"label": "View Lootboard", "action": {"type": "lootboard", "value": "7"},
                                  "icon": "lootboard"}
    assert card["recent"] == [{"x": 1}] and card["recent_group"] is True
    assert card["description"] == "Hi" and card["icon_path"] == "clans/7/icon.png"


def test_group_preview_says_what_is_still_loading():
    card = pc.group_card(name="Test Clan", group_id=7, members=123, rank=4, loot="1.25B", top_player=None,
                         partial=True)
    assert card["description"] == "Loading details…"
    assert card["recent"] is None and card["recent_empty"] == "Loading recent activity…"
    assert card["stats"][1]["value"] == "#4"
    # No top player: a dash, and nothing to click.
    assert card["stats"][3] == {"label": "Top Player", "value": "—", "action": None, "tip": None}
    # No invite code: no Discord button.
    assert [b["label"] for b in card["actions"]] == ["View Lootboard"]


def test_player_cards():
    reg = pc.player_card(name="Ra ine", player_id=3422, registered=True, total_loot="5.6M", rank=812,
                         points=1500, groups=[{"name": "DT Testers", "members": 37}, {"name": "GIMs", "members": None}],
                         recent=[])
    assert reg["subtitle"] == {"text": "Registered Player", "tone": "good", "tip": None}
    assert reg["list_title"] == "Groups:" and reg["list"] == ["- DT Testers (37 members)", "- GIMs"]
    assert [(s["label"], s["value"]) for s in reg["stats"]] == [
        ("Total Loot", "5.6M GP"), ("Global Rank", "#812"), ("Lifetime Points", "1500 pts")]
    assert reg["stats"][2]["action"] == {"type": "web", "value": "wiki/points"}
    assert reg["actions"][1]["action"] == {"type": "web", "value": "players/3422/view"}

    unreg = pc.player_card(name="Ra ine", player_id=None, registered=False, total_loot="0", rank=1, points=0)
    assert unreg["subtitle"]["tone"] == "warn" and "\n" in unreg["subtitle"]["tip"]
    assert unreg["list_title"] is None and unreg["list"] == []
    assert unreg["actions"][1]["action"]["value"] == "players/Ra ine/view"


def test_cards_only_use_actions_the_plugin_knows():
    cards = [pc.group_card(name="g", group_id=1, invite_code="x", top_player="p"),
             pc.player_card(name="p", player_id=1, registered=True, total_loot="1", rank=1, points=1)]
    for card in cards:
        assert {a["type"] for a in _all_actions(card)} <= ACTIONS
