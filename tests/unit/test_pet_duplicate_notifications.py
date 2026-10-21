"""Duplicate pet notifications (utils/pet_notifications.py), 2026-10-21.

Every duplicate pet used to be announced wherever ``notify_pets`` was on. A
group now decides, like it does for points: every duplicate (the default, so
nothing changes for anyone until they choose), none, or only chosen pets. A
duplicate goes out through the group's ``pet_dupe`` design when it has one,
else through its pet design.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import utils.pet_notifications as pn
from web_api import config_registry as reg
from web_api.config_registry import ConfigValidationError, coerce_to_storage


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class TestDuplicateFlag:
    @pytest.mark.parametrize("raw,expected", [
        (True, True), (False, False), ("true", True), ("false", False),
        ("True", True), ("1", True), ("0", False), (None, False), ("", False),
    ])
    def test_webhook_strings_are_read_properly(self, raw, expected):
        # "false" is a truthy string: a plain bool() called it a duplicate.
        assert pn.flag_is_true(raw) is expected

    def test_the_game_flag_wins_and_the_db_decides_without_it(self):
        assert pn.is_duplicate(is_new_pet=True, duplicate_flag="true") is True
        assert pn.is_duplicate(is_new_pet=False, duplicate_flag="false") is False
        assert pn.is_duplicate(is_new_pet=False) is True
        assert pn.is_duplicate(is_new_pet=True) is False


class TestAnnounced:
    def test_default_announces_every_duplicate(self):
        assert pn.DEFAULT_DUPLICATE_MODE == "all"
        assert pn.duplicate_announced(None, (), "Olmlet") is True
        assert pn.duplicate_announced("bogus", (), "Olmlet") is True

    def test_none_announces_nothing(self):
        assert pn.duplicate_announced("none", ("Olmlet",), "Olmlet") is False

    def test_selected_announces_listed_pets_only(self):
        assert pn.duplicate_announced("selected", ("Olmlet", "Vorki"), "olmlet") is True
        assert pn.duplicate_announced("selected", ("Olmlet",), "Vorki") is False
        assert pn.duplicate_announced("selected", (), "Vorki") is False

    def test_pet_list_parsing(self):
        assert pn.parse_pet_list('["Olmlet", "Vorki"]') == ("Olmlet", "Vorki")
        assert pn.parse_pet_list("Olmlet, Vorki") == ("Olmlet", "Vorki")
        assert pn.parse_pet_list("") == ()
        assert pn.parse_pet_list("[not json") == ()


def _config_session(rows):
    session = MagicMock()
    q = session.query.return_value
    q.filter.return_value = q
    q.all.return_value = [
        SimpleNamespace(config_key=k, config_value=v, long_value=lv) for k, v, lv in rows
    ]
    return session


class TestLoadSettings:
    def test_absent_rows_mean_every_duplicate(self):
        assert pn.load_settings(_config_session([]), 7) == ("all", ())

    def test_list_is_read_from_long_value(self):
        session = _config_session([
            ("notify_pet_duplicates", "selected", None),
            ("notify_pet_duplicate_pets", "", '["Olmlet","Vorki"]'),
        ])
        assert pn.load_settings(session, 7) == ("selected", ("Olmlet", "Vorki"))

    def test_seasonal_prefix_reads_the_mirror_keys(self):
        session = _config_session([("seasonal_notify_pet_duplicates", "none", None)])
        assert pn.load_settings(session, 7, "seasonal_") == ("none", ())

    def test_read_failure_announces(self):
        session = MagicMock()
        session.query.side_effect = RuntimeError("db down")
        assert pn.group_announces_duplicate(session, 7, "Olmlet") is True


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_fields_exist_with_seasonal_mirrors(self):
        mode = reg.get_config_field("notify_pet_duplicates")
        assert mode["type"] == "select" and mode["default"] == "all"
        assert mode["options"] == ["all", "none", "selected"]
        assert reg.get_config_field("notify_pet_duplicate_pets")["type"] == "petlist"
        assert "seasonal_notify_pet_duplicates" in reg.all_config_keys()
        assert "seasonal_notify_pet_duplicate_pets" in reg.all_config_keys()

    def test_mode_is_validated(self):
        assert coerce_to_storage("notify_pet_duplicates", "selected") == "selected"
        with pytest.raises(ConfigValidationError):
            coerce_to_storage("notify_pet_duplicates", "some")

    def test_pet_list_is_canonicalized_and_sorted(self):
        stored = coerce_to_storage("notify_pet_duplicate_pets", ["vorki", "Olmlet", "VORKI"])
        assert json.loads(stored) == ["Olmlet", "Vorki"]
        # The Discord panel's text box sends comma-separated names.
        assert json.loads(coerce_to_storage("notify_pet_duplicate_pets", "olmlet, vorki")) == [
            "Olmlet", "Vorki",
        ]
        assert coerce_to_storage("notify_pet_duplicate_pets", "") == ""
        assert coerce_to_storage("notify_pet_duplicate_pets", "[]") == ""

    def test_unknown_pets_and_bad_shapes_are_refused(self):
        with pytest.raises(ConfigValidationError, match="Not a pet"):
            coerce_to_storage("notify_pet_duplicate_pets", ["Olmlet", "Not a pet"])
        with pytest.raises(ConfigValidationError):
            coerce_to_storage("notify_pet_duplicate_pets", '{"a": 1}')
        with pytest.raises(ConfigValidationError):
            coerce_to_storage("notify_pet_duplicate_pets", 5)

    def test_long_lists_spill_to_long_value(self):
        from web_api.routes.config import LONG_VALUE_KEYS

        assert "notify_pet_duplicate_pets" in LONG_VALUE_KEYS
        assert "seasonal_notify_pet_duplicate_pets" in LONG_VALUE_KEYS


# --------------------------------------------------------------------------- #
# Processor gate
# --------------------------------------------------------------------------- #
class _FakePlayer:
    player_id = 5752782
    user_id = 99
    user = MagicMock()
    player_name = "D CAV"


def _session(player):
    session = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.filter_by.return_value = q
        q.order_by.return_value = q
        q.first.return_value = player if model is type(player) else None
        q.all.return_value = []
        return q

    session.query.side_effect = _query
    return session


def _drive(payload, settings):
    """Run pet_processor with notify_pets on and ``settings`` as the group's
    duplicate settings; return the group notifications it queued."""
    import data.submissions.pet as pet

    player = _FakePlayer()
    session = _session(player)
    notify = AsyncMock()
    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(pet, name, **kw))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_player_by_name_then_auth", new=AsyncMock(return_value=(player, True, True)))
        p("ensure_item_by_name", new=AsyncMock(return_value=MagicMock(item_id=20851)))
        p("ensure_npc_id_for_player", new=AsyncMock(return_value=(7550, "Great Olm")))
        p("ensure_can_create", new=AsyncMock(return_value=True))
        p("get_player_groups_with_global", new=MagicMock(
            return_value=[MagicMock(group_id=190, group_name="Clan")]))
        p("screenshot_required", new=AsyncMock(return_value=False))
        p("create_notification", new=notify)
        p("is_user_dm_enabled", new=MagicMock(return_value=False))
        p("get_config_prefix", new=MagicMock(return_value=""))
        stack.enter_context(
            patch("data.submissions.common.check_group_point_system_active", return_value=False))
        stack.enter_context(patch("utils.group_config.get_effective", return_value="1"))
        stack.enter_context(patch.object(pn, "load_settings", return_value=settings))
        stack.enter_context(patch("services.realtime.publish_feed_pet", create=True))
        stack.enter_context(patch("services.event_engine.queue_submission", create=True))
        asyncio.run(pet.pet_processor(payload, external_session=session))
    return [c for c in notify.await_args_list if c.args[0] == "pet"]


PAYLOAD = {
    "player_name": "D CAV", "acc_hash": "-537678130437333791",
    "pet_name": "Olmlet", "guid": "dupe-notify-guid", "source": "Chambers of Xeric",
}


class TestProcessorGate:
    def test_new_pet_is_announced_whatever_the_duplicate_setting(self):
        sent = _drive(dict(PAYLOAD, duplicate="false"), ("none", ()))
        assert len(sent) == 1
        assert sent[0].args[2]["is_duplicate"] is False

    def test_duplicate_announced_by_default(self):
        sent = _drive(dict(PAYLOAD, duplicate="true"), ("all", ()))
        assert len(sent) == 1
        assert sent[0].args[2]["is_duplicate"] is True

    def test_duplicate_suppressed_when_the_group_says_never(self):
        assert _drive(dict(PAYLOAD, duplicate=True), ("none", ())) == []

    def test_selected_mode(self):
        assert len(_drive(dict(PAYLOAD, duplicate=True), ("selected", ("Olmlet",)))) == 1
        assert _drive(dict(PAYLOAD, duplicate=True), ("selected", ("Vorki",))) == []


# --------------------------------------------------------------------------- #
# Clan broadcast line
# --------------------------------------------------------------------------- #
def test_clan_broadcast_flags_the_duplicate_line():
    from utils.clan_broadcasts import parse_broadcast

    dupe = parse_broadcast(
        "Grinder has a funny feeling like she would have been followed: Ikkle hydra at 5,000 kills."
    )
    new = parse_broadcast("Grinder has a funny feeling like she's being followed: Ikkle hydra.")
    assert dupe.extra.get("duplicate") is True
    assert "duplicate" not in new.extra


# --------------------------------------------------------------------------- #
# Which design a duplicate is sent with
# --------------------------------------------------------------------------- #
def _notification_service_module():
    # Loaded from its file like test_notification_default_embeds.py: conftest
    # stubs the ``services`` package.
    import importlib.util
    import os
    import sys

    name = "_pet_dupe_notification_service_under_test"
    if name in sys.modules:
        return sys.modules[name]
    for stub in ("services.contribution_notifications", "services.event_notifications"):
        sys.modules.setdefault(stub, MagicMock())
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "services", "notification_service.py",
    )
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _template(rows, custom_embeds, is_duplicate, group_id=190):
    ns = _notification_service_module()
    service = ns.NotificationService(MagicMock(), MagicMock())
    service.db_ops = MagicMock()
    service.db_ops.get_group_embed = AsyncMock(side_effect=lambda t, g: f"{t}@{g}")
    with patch.object(ns.NotificationService, "_has_embed_row",
                      side_effect=lambda _s, gid, t: (gid, t) in rows):
        return asyncio.run(service._pet_embed_template(MagicMock(), group_id, custom_embeds, is_duplicate))


class TestDuplicateDesign:
    def test_new_pet_uses_the_pet_design(self):
        assert _template({(190, "pet_dupe")}, True, False) == ("pet@190", False)
        assert _template(set(), False, False) == ("pet@1", False)

    def test_own_duplicate_design_wins(self):
        rows = {(190, "pet_dupe"), (190, "pet"), (1, "pet_dupe"), (1, "pet")}
        assert _template(rows, True, True) == ("pet_dupe@190", True)

    def test_own_pet_design_beats_the_default_duplicate_design(self):
        rows = {(190, "pet"), (1, "pet_dupe"), (1, "pet")}
        assert _template(rows, True, True) == ("pet@190", False)

    def test_default_duplicate_design_then_default_pet_design(self):
        assert _template({(1, "pet_dupe"), (1, "pet")}, True, True) == ("pet_dupe@1", False)
        assert _template({(1, "pet")}, True, True) == ("pet@1", False)

    def test_own_designs_need_the_entitlement(self):
        rows = {(190, "pet_dupe"), (190, "pet"), (1, "pet")}
        assert _template(rows, False, True) == ("pet@1", False)


def test_pet_dupe_fits_the_embed_type_column_and_is_a_layout_type():
    from web_api.routes.embeds import EMBED_TYPES, FALLBACK_EMBED_TYPES

    assert pn.DUPE_NOTIFICATION_TYPE in EMBED_TYPES
    assert FALLBACK_EMBED_TYPES[pn.DUPE_NOTIFICATION_TYPE] == "pet"
    assert all(len(t) <= 10 for t in EMBED_TYPES)  # group_embeds.embed_type VARCHAR(10)
    assert EMBED_TYPES.index("pet") < EMBED_TYPES.index("pet_dupe")  # listing relies on it

    from services.component_layout import DEFAULT_LAYOUTS, NOTIFICATION_TYPES, TYPE_META

    assert pn.DUPE_NOTIFICATION_TYPE in NOTIFICATION_TYPES
    assert "duplicate" in DEFAULT_LAYOUTS["pet_dupe"]["blocks"][0]["content"]
    assert TYPE_META["pet_dupe"]["tokens"] == TYPE_META["pet"]["tokens"]
