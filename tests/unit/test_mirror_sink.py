"""Rerouting mirrored production traffic on a dev instance.

When the admin panel switches on mirroring, the Cloudflare Worker sends a second
copy of every live submission to the dev box. Dev runs on a scrubbed copy of
production, so its database holds ~262 real ``groups.guild_id`` values and ~1,600
real channel ids — announcing a mirrored drop through the player's *actual*
groups would post into real clans' Discords using their thresholds.

``mirror_sink`` reroutes every notification to one dev group instead. The
ordering is the load-bearing part and is pinned below: the reroute happens
*before* the channel / hidden-player / blacklist gates, so those evaluate the
sink group's configuration rather than some real clan's.
"""

import asyncio

import pytest


@pytest.fixture()
def common():
    from data.submissions import common

    return common


@pytest.fixture()
def mc():
    """The leaf module the context actually lives in."""
    from utils import mirror_context

    return mirror_context


def _create(common, notification_type, group_id=None):
    """create_notification(...) with a player id that avoids the debug path."""
    return asyncio.run(
        common.create_notification(notification_type, 2, {}, group_id=group_id)
    )


@pytest.fixture()
def channel_gate(common, monkeypatch):
    """Record which group the first downstream gate is asked about, then stop.

    Returning False short-circuits create_notification immediately after the
    reroute, so the recorded group id is exactly what the rest of the function
    would have used.
    """
    seen = {}

    def fake_gate(db_session, group_id, notification_type):
        seen["group_id"] = group_id
        return False

    monkeypatch.setattr(common, "group_has_notification_channel", fake_gate)
    return seen


class TestReroute:
    def test_group_notification_is_rerouted_to_the_sink(self, common, channel_gate):
        with common.mirror_sink(99):
            assert _create(common, "drop", group_id=5) is None
        assert channel_gate["group_id"] == 99

    def test_without_a_sink_the_real_group_is_used(self, common, channel_gate):
        assert _create(common, "drop", group_id=5) is None
        assert channel_gate["group_id"] == 5

    def test_the_sinks_config_is_what_gets_evaluated(self, common, channel_gate):
        """The reason the reroute precedes the gates.

        If it happened after, a mirrored drop would be measured against the real
        group's minimum_value_to_notify and aimed at their channel — the
        "exceeds real groups' configurations" failure this exists to prevent.
        """
        with common.mirror_sink(99):
            _create(common, "drop", group_id=5)
        assert channel_gate["group_id"] != 5


class TestNeverDMsRealPeople:
    """A DM has no group to reroute to, and the recipient is a real person who
    did not ask to hear from the dev bot."""

    @pytest.mark.parametrize(
        "notification_type",
        ["dm_drop", "dm_pb", "dm_death", "dm_diary", "dm_quest", "dm_name_change"],
    )
    def test_dm_types_are_dropped(self, common, notification_type):
        with common.mirror_sink(99):
            assert _create(common, notification_type, group_id=5) is None

    def test_dm_is_dropped_rather_than_rerouted(self, common, channel_gate):
        """Not merely muted downstream — it must not reach the gates at all."""
        with common.mirror_sink(99):
            _create(common, "dm_drop", group_id=5)
        assert "group_id" not in channel_gate

    def test_dms_are_unaffected_without_a_sink(self, common, channel_gate):
        _create(common, "dm_drop", group_id=5)
        assert channel_gate["group_id"] == 5, "the gate is only bypassed for mirrored traffic"


class TestGrouplessNotifications:
    @pytest.mark.parametrize("group_id", [None, 0])
    def test_dropped_under_a_sink(self, common, group_id):
        """new_player / new_npc / name_change have nothing to reroute."""
        with common.mirror_sink(99):
            assert _create(common, "new_player", group_id=group_id) is None


class TestContextScoping:
    def test_sink_is_scoped_to_the_block(self, common, mc):
        with common.mirror_sink(99):
            assert mc.sink_group_id() == 99
            assert mc.is_mirrored_submission() is True
        assert mc.sink_group_id() is None
        assert mc.is_mirrored_submission() is False

    def test_nested_blocks_restore_the_outer_sink(self, common, mc):
        with common.mirror_sink(99):
            with common.mirror_sink(7):
                assert mc.sink_group_id() == 7
            assert mc.sink_group_id() == 99
        assert mc.sink_group_id() is None

    def test_common_reexports_the_same_context(self, common, mc):
        """The notification choke point reads it via common; everything else
        reads it via utils.mirror_context. They must be one context."""
        with common.mirror_sink(99):
            assert common.sink_group_id() == mc.sink_group_id() == 99

    def test_does_not_leak_across_concurrent_tasks(self, common, mc):
        """The ContextVar reason for existing.

        The consumer runs submissions as concurrent tasks. A module global here
        would mean a real submission announcing into the sink — or worse, a
        mirrored one escaping into a real group — depending on interleaving.
        """

        async def scenario():
            seen = {}

            async def mirrored():
                with common.mirror_sink(99):
                    await asyncio.sleep(0)
                    seen["mirrored"] = mc.sink_group_id()

            async def live():
                await asyncio.sleep(0)
                seen["live"] = mc.sink_group_id()

            await asyncio.gather(mirrored(), live())
            return seen

        seen = asyncio.run(scenario())
        assert seen["mirrored"] == 99
        assert seen["live"] is None, "live traffic must never see a sink"
