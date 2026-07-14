"""Tests for the Activity launcher's pure logic. Targets
``services.activity_launch_core`` (no ``interactions`` import), so it needs no
un-stubbing and can't contaminate other tests. The Discord shell
(services/activity_launch.py) is exercised live by a real launch click."""
import asyncio
import importlib.util
import os
from unittest.mock import AsyncMock, MagicMock


def _load_core():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "services", "activity_launch_core.py")
    spec = importlib.util.spec_from_file_location("_activity_launch_core_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = _load_core()


# --- interaction predicates ------------------------------------------------- #
def test_only_entry_point_command_matches():
    assert core.is_entry_point_interaction({"type": 2, "data": {"type": 4, "name": "launch"}})
    assert not core.is_entry_point_interaction({"type": 2, "data": {"type": 1, "name": "help"}})
    assert not core.is_entry_point_interaction({"type": 3, "data": {"custom_id": "x"}})
    assert not core.is_entry_point_interaction({})
    assert not core.is_entry_point_interaction(None)


def test_launch_button_interaction_matches():
    good = {"type": 3, "data": {"custom_id": core.LAUNCH_BUTTON_CUSTOM_ID}}
    assert core.is_launch_button_interaction(good)
    assert not core.is_launch_button_interaction({"type": 3, "data": {"custom_id": "other"}})
    assert not core.is_launch_button_interaction({"type": 2, "data": {"custom_id": core.LAUNCH_BUTTON_CUSTOM_ID}})
    assert not core.is_entry_point_interaction(good)  # a button is not the entry point


def test_event_scoped_launch_button_matches():
    scoped = {"type": 3, "data": {"custom_id": "activity_launch_open:e:42"}}
    assert core.is_launch_button_interaction(scoped)
    # a lookalike prefix without the separator is not a launch button
    assert not core.is_launch_button_interaction(
        {"type": 3, "data": {"custom_id": "activity_launch_open_evil"}}
    )


# --- launch custom_id round-trip + parsing ---------------------------------- #
def test_launch_custom_id_round_trip():
    assert core.launch_button_custom_id() == core.LAUNCH_BUTTON_CUSTOM_ID
    assert core.launch_button_custom_id(None) == core.LAUNCH_BUTTON_CUSTOM_ID
    assert core.launch_button_custom_id(0) == core.LAUNCH_BUTTON_CUSTOM_ID
    scoped = core.launch_button_custom_id(42)
    assert scoped == "activity_launch_open:e:42"
    assert core.parse_launch_custom_id(scoped) == "42"


def test_parse_launch_custom_id_rejects_non_events():
    assert core.parse_launch_custom_id(core.LAUNCH_BUTTON_CUSTOM_ID) is None  # bare card button
    assert core.parse_launch_custom_id("activity_launch_open:e:") is None
    assert core.parse_launch_custom_id("activity_launch_open:e:abc") is None
    assert core.parse_launch_custom_id("evtsignup:5") is None
    assert core.parse_launch_custom_id(None) is None


# --- interaction user id + intent handoff ----------------------------------- #
def test_interaction_user_id_guild_and_dm():
    assert core.interaction_user_id({"member": {"user": {"id": "111"}}}) == "111"
    assert core.interaction_user_id({"user": {"id": "222"}}) == "222"  # DM shape
    assert core.interaction_user_id({}) is None


def test_launch_intent_from_interaction():
    data = {"type": 3, "member": {"user": {"id": "111"}},
            "data": {"custom_id": "activity_launch_open:e:42"}}
    assert core.launch_intent_from_interaction(data) == ("111", "42")
    # bare card button carries no event → no intent to stash
    assert core.launch_intent_from_interaction(
        {"type": 3, "member": {"user": {"id": "111"}},
         "data": {"custom_id": core.LAUNCH_BUTTON_CUSTOM_ID}}
    ) is None
    # no user → nothing to key on
    assert core.launch_intent_from_interaction(
        {"type": 3, "data": {"custom_id": "activity_launch_open:e:42"}}
    ) is None


def test_intent_key():
    assert core.intent_key("111") == "dt:activity:launch:111"


# --- launch follow-up message (off by default) ------------------------------ #
def test_no_launch_message_by_default():
    assert core.SEND_LAUNCH_MESSAGE is False
    assert core.build_launch_message({}) is None


def test_launch_message_ephemeral_when_enabled(monkeypatch):
    monkeypatch.setattr(core, "SEND_LAUNCH_MESSAGE", True)
    payload = core.build_launch_message({})
    assert payload is not None and payload["embeds"][0]["title"]
    assert payload.get("flags") == core.MSG_FLAG_EPHEMERAL == 64


def test_public_message_has_no_flags(monkeypatch):
    monkeypatch.setattr(core, "SEND_LAUNCH_MESSAGE", True)
    monkeypatch.setattr(core, "LAUNCH_MESSAGE_EPHEMERAL", False)
    assert "flags" not in core.build_launch_message({})


def test_callback_constant_is_launch_activity():
    assert core.CALLBACK_LAUNCH_ACTIVITY == 12


# --- ref parsing ------------------------------------------------------------ #
def test_ref_parsing_and_channel_cleaning():
    assert core.parse_ref("123:456") == ("123", "456")
    assert core.parse_ref(None) == (None, None)
    assert core.parse_ref("garbage") == (None, None)
    assert core.clean_channel("123") == "123"
    assert core.clean_channel("0") is None
    assert core.clean_channel("") is None
    assert core.clean_channel(None) is None


# --- reconcile state machine (injected side effects) ------------------------ #
def _fx():
    """A reconcile fixture: mock session + async post/delete recorders."""
    session = MagicMock()
    post = AsyncMock(return_value=MagicMock(id=999))
    delete = AsyncMock()
    return session, post, delete


def _run(session, group_id, channel_id, ref_row, post, delete):
    # Run on an isolated loop and RESTORE the previous one — other tests in the
    # suite use asyncio.get_event_loop(), which asyncio.run() would clear.
    try:
        prev = asyncio.get_event_loop()
    except RuntimeError:
        prev = None
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            core.reconcile_group(
                session, group_id, channel_id, ref_row, post_card=post, delete_message=delete
            )
        )
    finally:
        loop.close()
        asyncio.set_event_loop(prev)


def test_reconcile_posts_when_new():
    session, post, delete = _fx()
    _run(session, 5, "42", None, post, delete)
    post.assert_awaited_once_with("42")
    delete.assert_not_awaited()
    session.add.assert_called_once()  # stored the new ref
    session.commit.assert_called()


def test_reconcile_noop_when_already_current():
    session, post, delete = _fx()
    _run(session, 5, "42", MagicMock(config_value="42:100"), post, delete)
    post.assert_not_awaited()  # nothing to do — no API call
    delete.assert_not_awaited()
    session.commit.assert_not_called()


def test_reconcile_moves_when_channel_changes():
    session, post, delete = _fx()
    ref = MagicMock(config_value="11:100")  # old channel 11
    _run(session, 5, "42", ref, post, delete)
    delete.assert_awaited_once_with("11", "100")  # removed stale card
    post.assert_awaited_once_with("42")  # posted in new channel
    assert ref.config_value == "42:999"  # ref updated in place


def test_reconcile_deletes_when_cleared():
    session, post, delete = _fx()
    ref = MagicMock(config_value="11:100")
    _run(session, 5, None, ref, post, delete)
    delete.assert_awaited_once_with("11", "100")
    session.delete.assert_called_once_with(ref)  # forgot the ref
    post.assert_not_awaited()
