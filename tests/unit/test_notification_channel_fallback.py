"""NotificationService._resolve_group_channel_id (services/notification_service.py).

Regression cover for collection-log notifications being dropped when a group
had `notify_clogs` on and a drops channel set but no dedicated clog channel.

The config editor documents "Falls back to the drops channel when unset" for
every per-type channel, and data/submissions/common.GROUP_CHANNEL_NOTIFICATION_KEYS
gates enqueueing on that same key pair — so a clog was queued as deliverable and
then failed at send time with "No channel configured for group N". The old
send-side guard returned before it could ever consult the loot channel; its
fallback branch was only reachable for the legacy "0" sentinel, never for an
empty value or a missing row. 387 clog notifications across 9 groups were lost
to this in the 30 days before the fix.

Loaded directly from the file path (like test_notification_channel_guard.py)
because conftest stubs the ``services`` package.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

for _name in (
    "services.contribution_notifications",
    "services.event_notifications",
):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "notification_service.py",
)
_spec = importlib.util.spec_from_file_location("_notification_service_fallback_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_notification_service_fallback_under_test"] = ns
_spec.loader.exec_module(ns)

NotificationService = ns.NotificationService

DROPS = "1534651425089392762"
DEDICATED = "1404694727395246180"


def _service():
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=SimpleNamespace(send=AsyncMock()))
    return NotificationService(bot, MagicMock())


def _db(rows):
    """rows: {config_key: config_value} for the GroupConfiguration rows that exist.

    A key absent from the dict models a missing row; a key mapped to "" or None
    models a row the group cleared in the config editor.
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(config_key=k, config_value=v) for k, v in rows.items()
    ]
    return db


def _resolve(rows, primary="channel_id_to_post_clog"):
    return _service()._resolve_group_channel_id(_db(rows), 303, primary)


class TestDedicatedChannelWins:
    def test_dedicated_channel_is_preferred_over_drops(self):
        assert _resolve({
            "channel_id_to_post_clog": DEDICATED,
            "channel_id_to_post_loot": DROPS,
        }) == DEDICATED

    def test_dedicated_channel_used_when_drops_is_unset(self):
        assert _resolve({
            "channel_id_to_post_clog": DEDICATED,
            "channel_id_to_post_loot": "",
        }) == DEDICATED


class TestFallsBackToDropsChannel:
    """Each of these returned "" before the fix, failing the notification."""

    def test_empty_dedicated_value_falls_back(self):
        # The exact production state of group 303 at 2026-08-05 20:28:15.
        assert _resolve({
            "channel_id_to_post_clog": "",
            "channel_id_to_post_loot": DROPS,
        }) == DROPS

    def test_missing_dedicated_row_falls_back(self):
        assert _resolve({"channel_id_to_post_loot": DROPS}) == DROPS

    def test_null_dedicated_value_falls_back(self):
        assert _resolve({
            "channel_id_to_post_clog": None,
            "channel_id_to_post_loot": DROPS,
        }) == DROPS

    def test_legacy_zero_sentinel_falls_back(self):
        assert _resolve({
            "channel_id_to_post_clog": "0",
            "channel_id_to_post_loot": DROPS,
        }) == DROPS

    def test_whitespace_only_dedicated_value_falls_back(self):
        assert _resolve({
            "channel_id_to_post_clog": "   ",
            "channel_id_to_post_loot": DROPS,
        }) == DROPS


class TestNothingConfigured:
    """Genuinely undeliverable — failing with "No channel configured" is correct."""

    def test_both_empty(self):
        assert _resolve({
            "channel_id_to_post_clog": "",
            "channel_id_to_post_loot": "",
        }) == ""

    def test_both_missing(self):
        assert _resolve({}) == ""

    def test_dedicated_empty_and_drops_is_zero_sentinel(self):
        assert _resolve({
            "channel_id_to_post_clog": "",
            "channel_id_to_post_loot": "0",
        }) == ""


class TestAppliesToEveryFallbackType:
    @pytest.mark.parametrize("primary", [
        "channel_id_to_post_clog",
        "channel_id_to_post_pb",
        "channel_id_to_post_ca",
    ])
    def test_empty_dedicated_falls_back_to_drops(self, primary):
        assert _resolve({primary: "", "channel_id_to_post_loot": DROPS}, primary) == DROPS

    @pytest.mark.parametrize("primary", [
        "channel_id_to_post_clog",
        "channel_id_to_post_pb",
        "channel_id_to_post_ca",
    ])
    def test_missing_dedicated_row_falls_back_to_drops(self, primary):
        assert _resolve({"channel_id_to_post_loot": DROPS}, primary) == DROPS


class TestAgreesWithEnqueueGate:
    """The send side must consider deliverable exactly what the enqueue gate does.

    When these disagree the notification is queued and then dropped — which is
    the bug this module covers. Both directions matter: enqueue-without-send
    loses notifications, send-without-enqueue means they never arrive at all.
    """

    @pytest.mark.parametrize("notification_type,primary", [
        ("clog", "channel_id_to_post_clog"),
        ("pb", "channel_id_to_post_pb"),
        ("ca", "channel_id_to_post_ca"),
    ])
    @pytest.mark.parametrize("dedicated", [None, "", "  ", "0", DEDICATED])
    @pytest.mark.parametrize("drops", [None, "", "  ", "0", DROPS])
    def test_deliverability_matches(self, notification_type, primary, dedicated, drops):
        common = pytest.importorskip("data.submissions.common")

        rows = {}
        if dedicated is not None:
            rows[primary] = dedicated
        if drops is not None:
            rows["channel_id_to_post_loot"] = drops

        send_side = bool(_resolve(rows, primary))
        gate_db = MagicMock()
        gate_db.query.return_value.filter.return_value.all.return_value = [
            (k, v) for k, v in rows.items()
        ]
        enqueue_side = common.group_has_notification_channel(gate_db, 303, notification_type)

        assert send_side == enqueue_side, (
            f"{notification_type}: enqueue gate says deliverable={enqueue_side} "
            f"but send side resolves {send_side!r} for {rows!r}"
        )

    def test_gate_key_pairs_match_the_keys_the_senders_resolve(self):
        common = pytest.importorskip("data.submissions.common")
        for notification_type, primary in (
            ("clog", "channel_id_to_post_clog"),
            ("pb", "channel_id_to_post_pb"),
            ("ca", "channel_id_to_post_ca"),
        ):
            assert common.GROUP_CHANNEL_NOTIFICATION_KEYS[notification_type] == (
                primary, "channel_id_to_post_loot",
            )
