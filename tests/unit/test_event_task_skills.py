"""Skill lists for xp/skill event tasks (Sailing) and the Runecraft tile icon."""
from web_api.routes.event_task_validation import OSRS_SKILLS
from web_api.task_tiles import icon_asset_path


def test_sailing_is_a_task_skill():
    assert "Sailing" in OSRS_SKILLS


def test_runecraft_tile_uses_the_wom_named_icon():
    assert icon_asset_path({"type": "skill", "name": "Runecraft"}) == "metrics/runecrafting.png"
    assert icon_asset_path({"type": "skill", "name": "Sailing"}) == "metrics/sailing.png"
    assert icon_asset_path({"type": "skill", "name": "Slayer"}) == "metrics/slayer.png"
