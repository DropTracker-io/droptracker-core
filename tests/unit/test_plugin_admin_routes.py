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
        "notify_pet_duplicate_pets": ["Not a pet"],  # petlist: checked like the website
        "export_api_key": "x",                    # sensitive
        "create_pb_embeds": True,                 # Hall of Fame
        "number_of_pbs_to_display": "lots",       # not a number
        "made_up_key": 1,
    }
    with patch.object(pa, "_hof_locked", return_value=True):
        errors = pa._validate(None, 0, 2, changes)
    assert "minimum_value_to_notify" not in errors
    assert "unknown pet" in errors["notify_pet_duplicate_pets"]
    assert errors["export_api_key"] == pa.ON_WEBSITE
    assert errors["create_pb_embeds"] == pa.LOCKED_HOF
    assert errors["made_up_key"].startswith("That setting doesn't exist")
    with patch.object(pa, "_hof_locked", return_value=False):
        bad = pa._validate(None, 0, 2, {"minimum_value_to_notify": -5})
    assert bad == {"minimum_value_to_notify": "Must be >= 0."}
    with patch.object(pa, "_hof_locked", return_value=False):
        assert "create_pb_embeds" not in pa._validate(None, 0, 2, {"create_pb_embeds": True})


# --------------------------------------------------------------------------- #
# Settings the website draws with its own picker
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stored_type,stored,client", [
    ("messagelist", '["{player_name} fell", "Oops"]', "{player_name} fell\nOops"),
    ("messagelist", "", ""),
    ("multiselect", "1,7", ["1", "7"]),
    ("multiselect", "", []),
    ("bosslist", "Vorkath, Zulrah", ["Vorkath", "Zulrah"]),
    ("petlist", '["Baby mole", "Vorki"]', ["Baby mole", "Vorki"]),
    ("petlist", "", []),
])
def test_stored_values_reach_the_plugin_as_its_controls_edit_them(stored_type, stored, client):
    assert pa._to_client(stored_type, stored) == client


def test_what_the_plugin_sends_is_put_back_in_the_stored_form():
    assert pa._from_client("messagelist", "  One \n\nTwo\r\n") == ["One", "Two"]
    assert pa._from_client("bosslist", ["Vorkath", " ", "Zulrah"]) == "Vorkath,Zulrah"
    assert pa._from_client("petlist", ["Vorki"]) == ["Vorki"]
    assert pa._from_client("multiselect", ["1"]) == ["1"]
    assert pa._from_client("int", 5) == 5


def test_picker_fields_save_through_the_registry_checks():
    with patch.object(pa, "_hof_locked", return_value=False):
        errors = pa._validate(None, 0, 2, {
            "death_message_variants": "Fine\n@everyone look",
            "slayer_excluded_masters": ["999"],
            "notify_pet_duplicate_pets": ["Vorki", "Baby mole"],
        })
    assert "mentions" in errors["death_message_variants"]
    assert "unknown option" in errors["slayer_excluded_masters"]
    assert "notify_pet_duplicate_pets" not in errors


def test_pet_choices_are_grouped_once_each():
    options = pa._picker_options(None, "petlist", {}, {})
    names = [o["value"] for o in options]
    assert len(names) == len(set(names)) and "Olmlet" in names
    assert {o["group"] for o in options} >= {"Boss pets", "Skilling pets"}


def test_the_settings_schema_draws_picker_fields_as_known_types():
    from web_api.config_registry import get_config_field

    assert {pa._AS_TYPE[get_config_field(k)["type"]] for k in (
        "loot_board_type", "death_message_variants", "slayer_excluded_masters",
        "personal_best_embed_boss_list", "notify_pet_duplicate_pets")} == {"select", "text", "multi"}
    assert set(pa._AS_TYPE.values()) <= pa.EDITABLE_TYPES


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #
def test_members_are_ranked_by_loot_and_a_search_keeps_the_rank():
    rows = [(1, "Zezima"), (2, "Lynx Titan"), (3, "B0aty"), (4, "Woox")]
    loot = {1: 5_000_000, 2: 0, 3: 12_500_000, 4: 5_000_000}
    page = pa._member_page(rows, loot, {2}, "", 1)
    assert [r["name"] for r in page["rows"]] == ["B0aty", "Woox", "Zezima", "Lynx Titan"]
    assert page["summary"] == "4 members, 1 hidden"
    assert page["rows"][0]["detail"] == "12.5M gp this month"
    assert page["rows"][3] == {"player_id": 2, "name": "Lynx Titan", "rank": 4,
                               "detail": "No loot this month", "hidden": True}
    found = pa._member_page(rows, loot, set(), "zez", 1)
    assert [(r["name"], r["rank"]) for r in found["rows"]] == [("Zezima", 3)]
    assert found["matches"] == 1 and found["summary"] == "4 members"


def test_members_page_numbers_are_clamped():
    rows = [(i, f"Player {i}") for i in range(pa.MEMBERS_PER_PAGE + 10)]
    last = pa._member_page(rows, {}, set(), "", 99)
    assert (last["page"], last["pages"], len(last["rows"])) == (2, 2, 10)
    first = pa._member_page(rows, {}, set(), "", 1)
    assert first["footer"] == f"Showing {pa.MEMBERS_PER_PAGE} of {pa.MEMBERS_PER_PAGE + 10}. Search to find anyone else."
    assert pa._member_page(rows[:5], {}, set(), "", 1)["footer"] is None
    assert pa._member_page([], {}, set(), "", 0)["pages"] == 1


@pytest.mark.parametrize("seconds,text", [(5, "just now"), (60, "1 minute ago"), (7200, "2 hours ago"),
                                          (86400 * 3, "3 days ago")])
def test_ago(seconds, text):
    from datetime import datetime, timedelta

    assert pa._ago(datetime.now() - timedelta(seconds=seconds)) == text
