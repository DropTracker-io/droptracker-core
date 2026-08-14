"""Tests for the Activity launcher's pure logic. Targets
``services.activity_launch_core`` (no ``interactions`` import), so it needs no
un-stubbing and can't contaminate other tests. The Discord shell
(services/activity_launch.py) is exercised live by a real launch click."""
import asyncio
import importlib.util
import os
from datetime import datetime
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


def test_launch_custom_id_view_round_trip():
    scoped = core.launch_button_custom_id(42, "review")
    assert scoped == "activity_launch_open:e:42:review"
    assert core.parse_launch_target(scoped) == ("42", "review")
    assert core.parse_launch_custom_id(scoped) == "42"
    # unknown views degrade to the plain event page, never reject the click
    assert core.parse_launch_target("activity_launch_open:e:42:bogus") == ("42", None)
    assert core.launch_button_custom_id(42, "bogus") == "activity_launch_open:e:42"
    assert core.parse_launch_target(core.LAUNCH_BUTTON_CUSTOM_ID) is None


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
    # view-scoped buttons stash "<event_id>:<view>" for the web_api claim
    assert core.launch_intent_from_interaction(
        {"type": 3, "member": {"user": {"id": "111"}},
         "data": {"custom_id": "activity_launch_open:e:42:review"}}
    ) == ("111", "42:review")


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
    assert core.CALLBACK_CHANNEL_MESSAGE == 4


# --- unsupported channel types + fallback message ---------------------------- #
def test_launch_supported_channel_types():
    for supported in (0, 1, 2, 3):  # text, DM, voice, group DM
        assert core.launch_supported_channel_type(supported)
    for unsupported in (5, 10, 11, 12, 13, 15, 16):  # announcement, threads, stage, forum, media
        assert not core.launch_supported_channel_type(unsupported)
    assert core.launch_supported_channel_type("11") is False  # int-able strings coerce
    # unknown shapes count as supported — the click-time fallback covers them
    assert core.launch_supported_channel_type(None)
    assert core.launch_supported_channel_type("garbage")


def test_interaction_channel_type():
    # raw interactions carry a partial channel object — the pre-check reads it
    assert core.interaction_channel_type({"channel": {"type": 11, "id": "1"}}) == 11
    assert core.interaction_channel_type({"channel": {"type": 0}}) == 0
    assert core.interaction_channel_type({"channel_id": "1"}) is None  # no channel object
    assert core.interaction_channel_type({"channel": "weird"}) is None
    assert core.interaction_channel_type(None) is None
    # end-to-end: a thread interaction must fail the launch pre-check
    assert not core.launch_supported_channel_type(
        core.interaction_channel_type({"channel": {"type": 11}})
    )
    # ...and a missing channel object must NOT block the launch
    assert core.launch_supported_channel_type(core.interaction_channel_type({}))


def test_activity_link_url():
    bare = core.activity_link_url()
    assert bare == f"https://discord.com/activities/{core.ACTIVITY_APP_ID}"
    assert core.activity_link_url(None) == bare
    assert core.activity_link_url(0) == bare
    assert core.activity_link_url(42) == f"{bare}?custom_id=e%3A42"


def test_launch_fallback_message_event_scoped():
    data = {"type": 3, "data": {"custom_id": "activity_launch_open:e:42"}}
    payload = core.build_launch_fallback_message(data)
    assert payload["flags"] == core.MSG_FLAG_EPHEMERAL
    app_btn, web_btn = payload["components"][0]["components"]
    assert app_btn["style"] == web_btn["style"] == 5  # URL buttons
    assert app_btn["url"] == core.activity_link_url("42")
    assert web_btn["url"] == f"{core.EVENT_BASE_URL}/42"


def test_launch_fallback_message_bare_launch():
    # entry point / bare card button — no event to deep-link to
    payload = core.build_launch_fallback_message({"type": 2, "data": {"type": 4}})
    assert payload["flags"] == core.MSG_FLAG_EPHEMERAL
    app_btn, web_btn = payload["components"][0]["components"]
    assert app_btn["url"] == core.activity_link_url()
    assert web_btn["url"] == core.WEBSITE_URL


# --- channel -> event fallback ordering -------------------------------------- #
NOW = datetime(2026, 8, 14, 12, 0)


def _ev(event_id, status, starts_at=None, ends_at=None):
    return {"id": event_id, "status": status, "starts_at": starts_at, "ends_at": ends_at}


def test_channel_event_prefers_the_soonest_upcoming_draft():
    """The reported bug: a group queued next month's event onto the channel its
    current one announces from, and the newest-id fallback hijacked every
    launch button on the running event's messages."""
    summers_end = _ev(46, "draft", datetime(2026, 8, 17), datetime(2026, 9, 1, 3, 59))
    september = _ev(51, "draft", datetime(2026, 9, 11, 14, 0), datetime(2026, 9, 19, 2, 0))
    assert core.pick_channel_event([summers_end, september], NOW) == 46
    assert core.pick_channel_event([september, summers_end], NOW) == 46


def test_channel_event_prefers_active_over_everything():
    running = _ev(2, "active", datetime(2026, 8, 1), datetime(2026, 8, 30))
    imminent = _ev(9, "draft", datetime(2026, 8, 14, 13, 0), datetime(2026, 8, 20))
    assert core.pick_channel_event([running, imminent], NOW) == 2
    # ...even with dates that don't place "now" inside its own window.
    assert core.pick_channel_event([_ev(2, "active"), imminent], NOW) == 2


def test_channel_event_keeps_a_just_ended_event_over_a_distant_draft():
    """An ended event's "Final standings" button must still land right."""
    ended = _ev(3, "past", datetime(2026, 8, 1), datetime(2026, 8, 13))
    far_off = _ev(8, "draft", datetime(2026, 11, 1), datetime(2026, 11, 20))
    assert core.pick_channel_event([ended, far_off], NOW) == 3
    # But once the next one is nearer than the last one's ending, it takes over.
    assert core.pick_channel_event([ended, _ev(8, "draft", datetime(2026, 8, 15))], NOW) == 8


def test_channel_event_lets_a_long_finished_event_go():
    """Past PAST_RELEVANCE_WINDOW an event stops shadowing the one being set
    up now — group 14's admin channel, shared by a July stress test and the
    September event whose Discord it was configured for."""
    stale = _ev(28, "past", datetime(2026, 7, 25), datetime(2026, 8, 4))
    upcoming = _ev(51, "draft", datetime(2026, 9, 11, 14, 0), datetime(2026, 9, 19, 2, 0))
    assert core.pick_channel_event([stale, upcoming], NOW) == 51
    # An undated draft has nothing to go on, so the stale event still wins.
    assert core.pick_channel_event([stale, _ev(51, "draft")], NOW) == 28


def test_channel_event_sorts_undated_drafts_last_and_dedupes():
    dated = _ev(4, "draft", datetime(2026, 9, 1))
    assert core.pick_channel_event([_ev(7, "draft"), dated], NOW) == 4
    # An event pointing several channel kinds at one channel repeats in the join.
    assert core.pick_channel_event([dated, dict(dated)], NOW) == 4
    # Nothing to pick from.
    assert core.pick_channel_event([], NOW) is None
    assert core.pick_channel_event(None, NOW) is None
    assert core.pick_channel_event([{"id": None, "status": "draft"}], NOW) is None
    # All undated: still answers, newest id wins.
    assert core.pick_channel_event([_ev(7, "draft"), _ev(9, "draft")], NOW) == 9


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
