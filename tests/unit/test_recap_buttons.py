"""The recap DM's button handlers (services/recap_buttons.py).

The handlers themselves need a Discord client; what is testable here is the
contract between them and the message that carries the buttons: the ids the
delivery module writes into a DM must be the ids this module routes on, and
the picker it builds must be the persistent, multi-select control the
handler expects back. Both modules are loaded from their file paths so the
conftest stubs don't shadow them.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# conftest stubs `interactions` as a flat MagicMock (not a package), so the
# module's `from interactions.api.events import ...` needs the subpath stubbed.
for _name in ("interactions.api", "interactions.api.events"):
    sys.modules.setdefault(_name, MagicMock())


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


delivery = _load("_recap_delivery_for_buttons_ut", ("services", "recap_delivery.py"))
# The handler imports the pure helpers lazily by their real name; point that
# name at the module loaded above so the stubbed `services` package is bypassed.
sys.modules["services.recap_delivery"] = delivery
buttons = _load("_recap_buttons_ut", ("services", "recap_buttons.py"))

PLAYERS = [(1, "Buzzyn", False), (2, "Buzzyn alt", False)]


def test_component_ids_match_the_message_builder():
    # recap_delivery cannot import this module (it would pull the Discord
    # client into the delivery script), so each keeps a copy — pinned here.
    assert buttons.OPT_IN_ID == delivery.OPT_IN_ID
    assert buttons.OPT_OUT_ID == delivery.OPT_OUT_ID
    assert buttons.ACCOUNT_PICK_PREFIX == delivery.ACCOUNT_PICK_PREFIX
    assert buttons.ACCOUNT_SET_ID == delivery.ACCOUNT_SET_ID
    assert buttons.CONFIG_KEY == delivery.USER_CFG_OPT_IN
    assert buttons.ACCOUNTS_KEY == delivery.USER_CFG_ACCOUNTS


def test_the_dm_button_routes_to_the_picker():
    msg = delivery.build_dm_message(
        delivery.UserTarget(user_id=1, discord_id="1", player_id=1,
                            player_name="Buzzyn", period="2026-08"),
        {}, None,
    )
    ids = [c.get("custom_id") for c in msg["components"][0]["components"]]
    assert any(i and i.startswith(buttons.ACCOUNT_PICK_PREFIX) for i in ids)


def test_picker_is_a_persistent_multi_select():
    buttons.StringSelectMenu.reset_mock()
    content, components = buttons.build_picker(PLAYERS, "", opted_in=True)
    kwargs = buttons.StringSelectMenu.call_args.kwargs
    # Persistent: the handler matches on this id, months later, after restarts.
    assert kwargs["custom_id"] == buttons.ACCOUNT_SET_ID
    # Nothing ticked has to be submittable — it is the opt-out.
    assert kwargs["min_values"] == 0
    # Two automatic options plus one per account.
    assert kwargs["max_values"] == 2 + len(PLAYERS)
    assert len(buttons.StringSelectMenu.call_args.args) == 2 + len(PLAYERS)
    assert len(components) == 1
    assert "Which accounts should get a monthly recap?" in content


def test_picker_says_what_the_default_meant_this_month():
    content, _ = buttons.build_picker(PLAYERS, "", opted_in=True, card_player_id=2)
    assert "biggest month" in content
    assert "This month that was **Buzzyn alt**" in content


def test_picker_names_the_current_choice():
    content, _ = buttons.build_picker(PLAYERS, "1,2", opted_in=True, card_player_id=1)
    assert "Currently: **Buzzyn** and **Buzzyn alt**." in content
    # Only the default needs explaining in terms of this month's card.
    assert "This month that was" not in content


def test_picker_tells_a_first_timer_it_is_off():
    content, _ = buttons.build_picker(PLAYERS, "", opted_in=False)
    assert "Currently: off" in content


def test_picker_note_goes_on_top():
    content, _ = buttons.build_picker(PLAYERS, "", opted_in=True, note="ℹ️ **No change**\n")
    assert content.startswith("ℹ️ **No change**")
