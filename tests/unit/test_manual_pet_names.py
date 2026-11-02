"""Typed pet names on manual submissions (ticket 505, 2026-10-30).

A member submitted "Smol" from the Submit panel. No item has that name, so
pet_processor recorded nothing (no player_pets row, no duplicate check) yet
announced the pet and answered "Pet processed successfully." Typed names are
now resolved to a real pet before intake (short names, punctuation, typos,
narrowed by where the pet came from), the panel asks "which pet?" with
buttons when a name could be several, and the processor refuses a manual pet
it can't place.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.submissions.manual_discord import resolve_pet_input
from utils.osrs_pets import pet_name_choices, resolve_pet_name

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, _ROOT.joinpath(*parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sp = _load("_sp_core_pet_names", "services", "submit_panel_core.py")


class TestResolvePetName:
    @pytest.mark.parametrize("typed, expected", [
        ("Smol heredit", "Smol heredit"),
        ("smol HEREDIT", "Smol heredit"),
        ("lil zik", "Lil' zik"),
        ("Vetion jr", "Vet'ion jr."),
        ("graardor", "Pet general graardor"),
        ("jad", "Tzrek-jad"),
        ("Olmet", "Olmlet"),
    ])
    def test_one_pet(self, typed, expected):
        assert resolve_pet_name(typed)[0] == expected

    def test_short_name_is_ambiguous_on_its_own(self):
        match, candidates = resolve_pet_name("Smol")
        assert match is None
        assert set(candidates) == {"Smol heredit", "Smolcano"}

    def test_where_it_came_from_narrows_it(self):
        assert resolve_pet_name("Smol", "Sol Heredit")[0] == "Smol heredit"
        assert resolve_pet_name("Smol", "Zalcano")[0] is None  # no shared word

    def test_skill_narrows_skilling_pets(self):
        assert resolve_pet_name("golem", "Mining")[0] == "Rock golem"

    def test_not_a_pet(self):
        assert resolve_pet_name("Abyssal whip") == (None, ())
        assert resolve_pet_name("") == (None, ())

    def test_recorded_names_cover_pets_the_catalogue_lacks(self):
        assert resolve_pet_name("zorbit") == (None, ())
        assert resolve_pet_name("zorbit", extra_names=("Zorbit",))[0] == "Zorbit"


class TestResolvePetInput:
    def test_match(self):
        assert resolve_pet_input("Smol", "Sol Heredit")[:2] == ("Smol heredit", None)

    def test_ambiguous_names_the_options(self):
        name, err, candidates = resolve_pet_input("Smol")
        assert name is None
        assert "Smol heredit" in err and "Smolcano" in err
        assert len(candidates) == 2

    def test_unknown_is_refused(self):
        name, err, candidates = resolve_pet_input("Abyssal whip", exact_item=lambda n: None)
        assert name is None and "don't know a pet" in err and candidates == ()

    def test_exact_item_lets_a_brand_new_pet_through(self):
        assert resolve_pet_input("Newpet", exact_item=lambda n: "Newpet")[:2] == ("Newpet", None)

    def test_copy_has_no_em_dashes(self):
        for typed in ("Smol", "pet", "zzzz"):
            err = resolve_pet_input(typed)[1]
            assert "—" not in err


class TestPetNameChoices:
    def test_prefix_first(self):
        assert pet_name_choices("smol") == ["Smol heredit", "Smolcano"]

    def test_punctuation_folded(self):
        assert pet_name_choices("lil z") == ["Lil' zik"]


class TestPanelPieces:
    def test_pick_buttons_round_trip(self):
        rows = sp.pet_pick_components("tok-1_x", ["Smol heredit", "Smolcano"])
        buttons = rows[0]["components"]
        assert [b["label"] for b in buttons] == ["Smol heredit", "Smolcano"]
        assert sp.parse_pet_pick(buttons[1]["custom_id"]) == ("tok-1_x", 1)
        assert all(len(b["custom_id"]) <= 100 for b in buttons)

    def test_pick_buttons_capped(self):
        rows = sp.pet_pick_components("t", [f"p{i}" for i in range(9)])
        assert len(rows[0]["components"]) == sp.PET_PICK_MAX

    @pytest.mark.parametrize("custom_id", ["", "dtsubmit:open", "dtsubmit:petpick:", "dtsubmit:petpick:t:x"])
    def test_bad_pick_ids(self, custom_id):
        assert sp.parse_pet_pick(custom_id) is None

    def test_pet_modal_explains_the_fields(self):
        fields = sp._type_fields("pet")
        assert all(len(f.get("description", "")) <= 100 for f in fields)
        assert fields[0].get("description") and fields[1].get("description")


# --------------------------------------------------------------------------- #
# Processor: a manual pet it can't place is refused, not announced
# --------------------------------------------------------------------------- #
class _FakePlayer:
    player_id = 5773202
    user_id = 4542
    user = MagicMock()
    player_name = "inversify"


def _drive(payload, item):
    import data.submissions.pet as pet

    player = _FakePlayer()
    session = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.first.return_value = None
    session.query.return_value = q
    notify = AsyncMock()
    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(pet, name, **kw))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_player_by_name_then_auth", new=AsyncMock(return_value=(player, True, True)))
        p("ensure_item_by_name", new=AsyncMock(return_value=item))
        p("ensure_npc_id_for_player", new=AsyncMock(return_value=(12821, "Sol Heredit")))
        p("ensure_can_create", new=AsyncMock(return_value=True))
        p("get_player_groups_with_global", new=MagicMock(
            return_value=[MagicMock(group_id=2, group_name="DropTracker.io")]))
        p("screenshot_required", new=AsyncMock(return_value=False))
        p("create_notification", new=notify)
        p("is_user_dm_enabled", new=MagicMock(return_value=False))
        p("get_config_prefix", new=MagicMock(return_value=""))
        stack.enter_context(
            patch("data.submissions.common.check_group_point_system_active", return_value=False))
        stack.enter_context(patch("utils.group_config.get_effective", return_value="1"))
        stack.enter_context(patch("services.realtime.publish_feed_pet", create=True))
        queued = stack.enter_context(patch("services.event_engine.queue_submission", create=True))
        result = asyncio.run(pet.pet_processor(payload, external_session=session))
    return result, notify, queued


PAYLOAD = {"player_name": "inversify", "acc_hash": "-1538971926209061728",
           "pet_name": "Smol", "guid": "ticket-505", "source": "Sol Heredit"}


class TestProcessorGuard:
    def test_manual_unknown_pet_is_refused(self):
        result, notify, queued = _drive(dict(PAYLOAD, intake_source="manual"), None)
        assert result.success is False
        assert "Smol" in result.message
        notify.assert_not_awaited()
        queued.assert_not_called()

    def test_plugin_pet_without_an_item_still_announced(self):
        result, notify, _ = _drive(dict(PAYLOAD), None)
        assert result.success is True
        assert any(c.args[0] == "pet" for c in notify.await_args_list)

    def test_manual_known_pet_goes_through(self):
        item = SimpleNamespace(item_id=28960)
        result, notify, _ = _drive(dict(PAYLOAD, pet_name="Smol heredit",
                                        intake_source="manual"), item)
        assert result.success is True
        assert any(c.args[0] == "pet" for c in notify.await_args_list)


# --------------------------------------------------------------------------- #
# A 403 must reach the shared "the bot can't post here" alert
# --------------------------------------------------------------------------- #
def test_no_sender_swallows_a_forbidden():
    """process_notification_with_session raises the group notice and DMs the
    admins on interactions.errors.Forbidden. A sender whose catch-all handler
    neither re-raises nor lets Forbidden past it hides the broken channel
    (the pet sender did, ticket 505)."""
    import ast

    tree = ast.parse((_ROOT / "services" / "notification_service.py").read_text())
    swallowing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef)
                and node.name.startswith("send_") and node.name.endswith("_with_session")):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Try):
                continue
            names = [ast.unparse(h.type) if h.type is not None else "" for h in stmt.handlers]
            if any("Forbidden" in n for n in names):
                continue
            for handler, name in zip(stmt.handlers, names):
                if name in ("Exception", "") and not any(
                        isinstance(n, ast.Raise) for n in ast.walk(handler)):
                    swallowing.append(node.name)
    assert swallowing == []
