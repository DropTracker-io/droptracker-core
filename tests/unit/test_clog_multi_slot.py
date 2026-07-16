"""
Regression tests for multi-slot collection log items in
data/submissions/clog.py.

Some collection-log slots share a single display name across many distinct
in-game item IDs — e.g. every Chompy bird hat milestone (Ogre bowman ...
Expert dragon archer) and the ~50 "Ancient page" variants. The plugin resolves
item_id by name, so every variant collapses onto ONE item_id. The processor's
normal (player_id, item_id) slot-dedup therefore treated a genuinely new slot as
an already-owned duplicate and silently dropped the notification (the webhook
still returned 200, so the client reported "processed").

The fix: for names in MULTI_SLOT_CLOG_ITEM_NAMES, skip slot-dedup so each new
unlock is recorded and notified; exact re-sends/retries are still caught by the
unique_id (guid) check in ensure_can_create().
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import data.submissions.clog as clog


def _run_processor(item_name, dedup_query_returns_existing):
    """Drive clog_processor for `item_name`, mocking every boundary.

    `dedup_query_returns_existing=True` simulates the player already having a
    row for this (player_id, item_id). Returns the create_notification mock so
    the caller can assert whether a notification was queued.
    """
    session = MagicMock()
    # Every query(...).filter(...).first() resolves truthy: the player re-fetch
    # (clog.py) and, for non-multi-slot items, the slot-dedup lookup both need a
    # row back. A truthy row means "slot already owned".
    existing = MagicMock() if dedup_query_returns_existing else None
    session.query.return_value.filter.return_value.first.return_value = existing or MagicMock()

    # A real class (not a bare MagicMock) so `type(player).player_id` — used by
    # the player re-fetch in clog_processor — resolves to a class attribute.
    class _FakePlayer:
        player_id = 4137
        user_id = 99
        user = MagicMock()

    player = _FakePlayer()
    group = MagicMock()
    group.group_id = 2
    group.group_name = "Global"

    item = MagicMock()
    item.item_id = 2978

    create_notification = AsyncMock()

    clog_data = {
        "player_name": "MX PIaid",
        "acc_hash": "-5376781304373337914",
        "item": item_name,
        "guid": f"guid-for-{item_name}",
        "source": "unknown",
        "kc": 0,
        "p_v": "5.3.0",
    }

    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(clog, name, **kw))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_item_by_name", new=AsyncMock(return_value=item))
        p("ensure_can_create", new=AsyncMock(return_value=True))
        p("ensure_player_by_name_then_auth", new=AsyncMock(return_value=(player, True, True)))
        p("ensure_npc_id_for_player", new=AsyncMock(return_value=(13943, "unknown")))
        p("get_player_groups_with_global", new=MagicMock(return_value=[group]))
        p("screenshot_required", new=AsyncMock(return_value=False))
        p("create_notification", new=create_notification)
        p("is_user_dm_enabled", new=MagicMock(return_value=False))
        p("award_points_to_player", new=MagicMock())
        p("get_config_prefix", new=MagicMock(return_value=""))
        stack.enter_context(
            patch("data.submissions.common.check_group_point_system_active", return_value=False)
        )
        stack.enter_context(patch("utils.group_config.get", return_value="true"))
        stack.enter_context(patch("utils.group_config.is_truthy", return_value=True))

        import asyncio
        # asyncio.run() is self-contained (own loop, closed on exit) so it does
        # not depend on ambient loop state left by earlier tests. The conftest
        # ensure_event_loop guard covers the general case; this removes the
        # fragile get_event_loop() pattern at its source.
        asyncio.run(clog.clog_processor(clog_data, external_session=session))

    return create_notification, session


class TestMultiSlotClog:
    @pytest.mark.parametrize(
        "name",
        [
            "Chompy bird hat", "Ancient page", "Mysterious page", "Medallion fragment",
            "Graceful hood", "Graceful top", "Graceful legs", "Graceful cape",
            "Graceful gloves", "Graceful boots",
            "Decorative helm", "Decorative full helm", "Decorative body",
            "Decorative legs", "Decorative skirt", "Decorative sword",
            "Decorative shield", "Decorative boots", "Decorative armour",
        ],
    )
    def test_names_are_registered_multi_slot(self, name):
        assert clog.is_multi_slot_clog_item(name)

    def test_matching_is_case_and_whitespace_insensitive(self):
        # e.g. the client/DB casing differs ("Mysterious page" vs "Mysterious Page")
        assert clog.is_multi_slot_clog_item("  chompy BIRD hat ")
        assert clog.is_multi_slot_clog_item("Mysterious Page")
        assert not clog.is_multi_slot_clog_item("Twisted bow")
        assert not clog.is_multi_slot_clog_item(None)
        assert not clog.is_multi_slot_clog_item("")

    def test_multi_slot_item_notifies_even_when_a_row_already_exists(self):
        """The core bug: a new Chompy bird hat slot must notify even though the
        player already has a (player_id, item_id) row for a *previous* hat."""
        create_notification, session = _run_processor(
            "Chompy bird hat", dedup_query_returns_existing=True
        )
        assert create_notification.await_count >= 1, (
            "new multi-slot clog slot should still create a group notification"
        )
        session.add.assert_called()  # a new row was recorded

    def test_normal_item_with_existing_row_is_deduped(self):
        """Control: an ordinary item the player already owns must NOT re-notify."""
        create_notification, session = _run_processor(
            "Twisted bow", dedup_query_returns_existing=True
        )
        assert create_notification.await_count == 0, (
            "already-owned ordinary slot must not re-notify"
        )
        session.add.assert_not_called()
