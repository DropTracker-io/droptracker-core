"""web138a group setup alerts: the pure policy pieces."""
import importlib.util
import os
import sys
from datetime import datetime, timedelta


def _load():
    # conftest stubs the `services` package; load the real module by path.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        "_setup_alerts_under_test", os.path.join(root, "services", "setup_alerts.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


sa = _load()
MAX_SENDS, AlertRow, build_message = sa.MAX_SENDS, sa.AlertRow, sa.build_message
diff_presence, invite_url, is_channel_error, mode = sa.diff_presence, sa.invite_url, sa.is_channel_error, sa.mode
recipient_order, should_send, snapshot_trustworthy = sa.recipient_order, sa.should_send, sa.snapshot_trustworthy

NOW = datetime(2026, 10, 21, 12, 0)


def test_presence_diff():
    assert diff_presence(None, {"1", "2"}) == (set(), set())  # first snapshot: nothing inferred
    assert diff_presence({"1", "2"}, {"2", "3"}) == ({"3"}, {"1"})
    assert diff_presence({"1"}, {"1"}) == (set(), set())


def test_collapsed_snapshot_is_not_trusted():
    prev = {str(i) for i in range(400)}
    assert snapshot_trustworthy(prev, {str(i) for i in range(395)})
    assert not snapshot_trustworthy(prev, {str(i) for i in range(100)})
    assert snapshot_trustworthy(None, set())
    assert snapshot_trustworthy({"1", "2"}, set())  # tiny installs (dev) are never "collapsed"


def test_channel_errors():
    assert is_channel_error("404 Not Found (error code: 10003): Unknown Channel")
    assert is_channel_error("403 Forbidden: Missing Access")
    assert is_channel_error("Channel not found")
    assert not is_channel_error("transient, retrying: timeout")
    assert not is_channel_error(None)


def test_send_policy():
    assert should_send("bot_missing", AlertRow("open", 0, None), NOW)
    assert not should_send("bot_missing", AlertRow("baseline", 0, None), NOW)
    assert not should_send("bot_missing", AlertRow("resolved", 0, None), NOW)
    # one reminder, after the interval, then nothing
    assert not should_send("bot_missing", AlertRow("open", 1, NOW - timedelta(days=1)), NOW)
    assert should_send("bot_missing", AlertRow("open", 1, NOW - timedelta(days=3, minutes=1)), NOW)
    assert not should_send("bot_missing", AlertRow("open", MAX_SENDS, NOW - timedelta(days=30)), NOW)


def test_recipient_order_owner_first_then_server_owner_then_admins():
    assert recipient_order("o", "g", ["a1", "a2"]) == ["o", "g", "a1", "a2"]
    assert recipient_order(None, "g", ["a1"]) == ["g", "a1"]
    assert recipient_order("o", "o", ["o", "a1"]) == ["o", "a1"]  # de-duplicated
    assert recipient_order(None, None, []) == []


def test_messages_are_plain_and_link_somewhere_useful():
    for kind in ("bot_missing", "posts_failing", "no_channel", "install_no_group"):
        msg = build_message(kind, group_id=14, group_name="Pegasus", guild_id="123", guild_name="Pegasus HQ",
                            detail="Missing Access", reminder=False)
        assert "—" not in msg  # site copy rule
        assert len(msg) <= 1900
        assert "https://" in msg
    assert "guild_id=123" in build_message("bot_missing", group_id=14, group_name="P", guild_id="123",
                                            guild_name=None, detail=None, reminder=False)
    assert build_message("no_channel", group_id=1, group_name="P", guild_id=None, guild_name=None,
                         detail=None, reminder=True).startswith("Reminder: ")


def test_invite_url_targets_the_server():
    url = invite_url("999")
    assert "permissions=268553232" in url and "guild_id=999" in url and "disable_guild_select=true" in url


def test_mode_defaults(monkeypatch):
    monkeypatch.delenv("SETUP_ALERTS_MODE", raising=False)
    monkeypatch.setenv("STATE", "dev")
    monkeypatch.delenv("STATUS", raising=False)
    assert mode() == "log"
    monkeypatch.setenv("STATE", "live")
    assert mode() == "send"
    monkeypatch.setenv("SETUP_ALERTS_MODE", "off")
    assert mode() == "off"
