"""Group-scoped member hiding must mute that group's Discord notifications.

Group leaders hide a member from the admin member listing (PATCH
/api/v1/groups/{id}/hidden-players → an ``ignored_players`` row). The lootboard
generators have always filtered on it, but the notification pipeline never
consulted it, so a hidden member's drops kept getting announced in the group's
Discord channel.

The guard lives in ``create_notification`` — the single chokepoint every
submission processor (drop, pb, clog, ca, pet, quest, death, diary, …) routes
through — so covering it here covers all of them.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _db(hidden_pairs=(), raises=False):
    """A session whose ignored_players lookups resolve against `hidden_pairs`.

    hidden_pairs: iterable of (group_id, player_id) the group has hidden.
    raises: model a DB fault so the fail-open path can be exercised.
    """
    hidden = {(int(g), int(p)) for g, p in hidden_pairs}
    db = MagicMock()

    def _query(*args, **kwargs):
        if raises:
            raise RuntimeError("database went away")
        q = MagicMock()

        def _filter(*criteria):
            # The MagicMock criteria carry no readable values, so the lookup is
            # keyed off the arguments the helper was called with instead — see
            # the closure over `probe` in each test below.
            f = MagicMock()
            f.first.return_value = (
                SimpleNamespace(id=1) if probe["pair"] in hidden else None
            )
            return f

        q.filter.side_effect = _filter
        return q

    probe = {"pair": None}
    db.query.side_effect = _query
    db._probe = probe
    return db


def _is_hidden(db, group_id, player_id):
    from data.submissions.common import player_hidden_for_group

    db._probe["pair"] = (int(group_id), int(player_id))
    return player_hidden_for_group(db, group_id, player_id)


class TestPlayerHiddenForGroup:
    def test_hidden_member_is_reported_hidden(self):
        db = _db([(303, 77)])
        assert _is_hidden(db, 303, 77) is True

    def test_visible_member_is_not_hidden(self):
        db = _db([(303, 77)])
        assert _is_hidden(db, 303, 88) is False

    def test_hiding_is_scoped_to_the_group_that_hid_them(self):
        # Group 303 hid the player; group 404 did not and must still announce.
        db = _db([(303, 77)])
        assert _is_hidden(db, 404, 77) is False

    def test_no_group_id_is_never_filtered(self):
        # DM / system notifications arrive with group_id 0 or None.
        db = _db([(303, 77)])
        for group_id in (0, None):
            from data.submissions.common import player_hidden_for_group

            assert player_hidden_for_group(db, group_id, 77) is False

    def test_db_error_fails_open(self):
        # A transient fault must not silently mute every group's notifications.
        db = _db([(303, 77)], raises=True)
        assert _is_hidden(db, 303, 77) is False


class TestCreateNotificationHonoursHiding:
    """The guard as the processors actually hit it."""

    @pytest.fixture
    def common(self):
        import data.submissions.common as common

        return common

    async def _create(self, common, hidden, group_id=303, player_id=77,
                      notification_type="drop"):
        db = MagicMock()
        with patch.object(common, "group_has_notification_channel", return_value=True), \
             patch.object(common, "player_hidden_for_group", return_value=hidden), \
             patch.object(common, "NotificationQueue", MagicMock()):
            common.stored_notifications = {}
            return await common.create_notification(
                notification_type,
                player_id,
                {"item_name": "Twisted bow"},
                group_id,
                existing_session=db,
            ), db

    async def test_hidden_player_enqueues_nothing(self, common):
        result, db = await self._create(common, hidden=True)
        assert result is None
        db.add.assert_not_called()

    async def test_visible_player_still_enqueues(self, common):
        result, db = await self._create(common, hidden=False)
        assert db.add.called

    @pytest.mark.parametrize(
        "notification_type", ["drop", "pb", "clog", "ca", "pet", "quest", "death", "diary"]
    )
    async def test_every_submission_type_is_covered(self, common, notification_type):
        _, db = await self._create(
            common, hidden=True, notification_type=notification_type
        )
        db.add.assert_not_called()

    async def test_personal_dm_survives_group_hiding(self, common):
        # dm_* notifications carry no group_id, so the guard never sees them:
        # hiding a member from a group must not cost them their own DMs.
        db = MagicMock()
        with patch.object(common, "group_has_notification_channel", return_value=True), \
             patch.object(common, "NotificationQueue", MagicMock()):
            common.stored_notifications = {}
            await common.create_notification(
                "dm_drop",
                77,
                {"item_name": "Twisted bow"},
                None,
                existing_session=db,
            )
        assert db.add.called
