"""Raw interaction readers survive interactions.py popping ``member.user``.

The library hands raw listeners SHALLOW copies and its Member parser pops
``member["user"]`` (leaving ``member["id"]``) from the shared dict, so every
raw reader must accept both shapes. Before the shared helper only the submit
panel did: review buttons lost the clicker (linked admins refused, audit
blank), Activity deep links lost their target, and relayed clicks were
refused outright."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from utils.interaction_payload import interaction_user_id

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, _ROOT.joinpath(*parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INTACT = {"member": {"user": {"id": "42"}, "roles": ["7"], "permissions": "32"},
          "guild_id": "1"}
POPPED = {"member": {"id": "42", "bot": False, "roles": ["7"], "permissions": "32"},
          "guild_id": "1"}
DM = {"user": {"id": "42"}}


@pytest.mark.parametrize("payload", [INTACT, POPPED, DM], ids=["intact", "popped", "dm"])
def test_helper_reads_every_shape(payload):
    assert interaction_user_id(payload) == "42"


@pytest.mark.parametrize("payload", [None, {}, {"member": {}}, "x"])
def test_helper_none_when_absent(payload):
    assert interaction_user_id(payload) is None


@pytest.mark.parametrize("payload", [INTACT, POPPED, DM], ids=["intact", "popped", "dm"])
def test_every_raw_reader_agrees(payload):
    launch = _load("_alc_payload_test", "services", "activity_launch_core.py")
    submit = _load("_spc_payload_test", "services", "submit_panel_core.py")
    relay = _load("_relay_core_payload_test", "services", "discord_relay_core.py")
    assert launch.interaction_user_id(payload) == "42"
    assert submit.interaction_user_id(payload) == "42"
    assert relay.interaction_facts(payload)["user_id"] == "42"


def test_review_facts_keep_the_clicker_after_the_pop():
    src = (_ROOT / "services" / "review_buttons_discord.py").read_text()
    ns: dict = {}
    start = src.index("def _facts(")
    end = src.index("\ndef ", start + 1)
    exec(src[start:end], ns)  # the function alone: the module imports interactions
    facts = ns["_facts"](dict(POPPED))
    assert facts["discord_id"] == "42"
    assert facts["roles"] == ["7"] and facts["permissions"] == "32"


def test_relay_accepts_a_popped_click():
    relay = _load("_relay_core_payload_test2", "services", "discord_relay_core.py")
    payload = dict(POPPED, id="1", token="t", type=3, application_id="9",
                   guild_id="1", data={"custom_id": "evtsignup:5"})
    assert relay.validate_interaction(payload, "9", ["1"]) == ("ephemeral", None)
