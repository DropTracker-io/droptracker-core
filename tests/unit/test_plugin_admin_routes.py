"""api/routes/plugin_admin.py: the settings schema and save checks the
plugin's group manager is drawn from.

The end-to-end behaviour (link, sessions, overview, saving through the
website's PATCH) was exercised on the dev instance; these pin the pieces that
decide what a leader may see and change in game.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pa = pytest.importorskip("api.routes.plugin_admin")


def test_validation_messages_read_without_the_key_name():
    assert pa._sentence("minimum_value_to_notify", "'minimum_value_to_notify' must be >= 0.") == "Must be >= 0."
    assert pa._sentence("x", "Pick a channel.") == "Pick a channel."
    assert pa._sentence("x", None) == "That value isn't allowed."


@pytest.mark.parametrize("value,text", [(0, "0"), (999, "999"), (12_500, "12.5K"), (1_250_000, "1.2M"),
                                        (3_000_000_000, "3B")])
def test_gp(value, text):
    assert pa._gp(value) == text


def test_channels_are_bare_names_grouped_by_kind_and_skip_categories():
    cache = [
        {"id": "1", "name": "drops", "type": "text"},
        {"id": "2", "name": "Info", "type": "category"},
        {"id": "3", "name": "pbs", "type": "forum"},
        {"id": "4", "name": "zulrah", "type": "thread", "parent_id": "3"},
        {"id": "5", "name": "Loot: 1B", "type": "voice"},
    ]
    with patch.object(pa, "_cache_json", return_value=cache):
        out = pa._channels("123")
    assert out == [
        {"value": "1", "label": "drops", "group": "Channels"},
        {"value": "3", "label": "pbs", "group": "Forums"},
        {"value": "4", "label": "pbs › zulrah", "group": "Threads"},
        {"value": "5", "label": "Loot: 1B", "group": "Voice channels"},
    ]
    assert pa._channels(None) == []


def test_only_plugin_drawn_types_can_be_saved_and_premium_keys_stay_locked():
    changes = {
        "minimum_value_to_notify": 1000,          # int: fine
        "notify_pet_duplicate_pets": "[]",        # petlist: website only
        "export_api_key": "x",                    # sensitive
        "create_pb_embeds": True,                 # Hall of Fame
        "number_of_pbs_to_display": "lots",       # not a number
        "made_up_key": 1,
    }
    with patch.object(pa, "_hof_locked", return_value=True):
        errors = pa._validate(None, 0, 2, changes)
    assert "minimum_value_to_notify" not in errors
    assert errors["notify_pet_duplicate_pets"] == pa.ON_WEBSITE
    assert errors["export_api_key"] == pa.ON_WEBSITE
    assert errors["create_pb_embeds"] == pa.LOCKED_HOF
    assert errors["made_up_key"].startswith("That setting doesn't exist")
    with patch.object(pa, "_hof_locked", return_value=False):
        bad = pa._validate(None, 0, 2, {"minimum_value_to_notify": -5})
    assert bad == {"minimum_value_to_notify": "Must be >= 0."}
    with patch.object(pa, "_hof_locked", return_value=False):
        assert "create_pb_embeds" not in pa._validate(None, 0, 2, {"create_pb_embeds": True})
