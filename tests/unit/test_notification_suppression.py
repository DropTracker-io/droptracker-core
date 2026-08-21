"""Context-scoped notification muting (data/submissions/common.py).

Backfills need to re-record submissions without re-announcing them: replaying
an outage window through the normal intake path is the only way to recover
webhook-path submissions (their sole copy is the Discord message), but firing
the matching announcements hours late is noise rather than recovery.

``suppress_notifications`` gates ``create_notification`` at its single choke
point — every processor routes through it — and the guard is the first
statement in the function, so a muted type returns before any DB work.

It is a ContextVar rather than a module global on purpose: the consumers run
submissions as concurrent asyncio tasks, and a global would leak the mute from
a backfill into live traffic sharing the process.
"""

import asyncio

import pytest


@pytest.fixture()
def common():
    from data.submissions import common

    return common


def _create(common, notification_type):
    """create_notification(...) with the args the muted path never looks at."""
    return asyncio.run(common.create_notification(notification_type, 1, {}))


class TestSuppressNotifications:
    def test_muted_type_returns_none(self, common):
        with common.suppress_notifications("pb", "dm_pb"):
            assert _create(common, "pb") is None
            assert _create(common, "dm_pb") is None

    def test_mute_is_scoped_to_the_block(self, common):
        with common.suppress_notifications("pb"):
            assert _create(common, "pb") is None
        # Outside the block the type is live again. Anything other than the
        # early return means the guard let it through to the real body.
        assert common._suppressed_notification_types.get() == frozenset()

    def test_unmuted_types_are_not_short_circuited(self, common):
        # 'drop' is not muted, so it must NOT hit the early return — it should
        # proceed into the body (and fail there against the stubbed DB, which
        # is exactly the proof that it was not silently swallowed).
        with common.suppress_notifications("pb"):
            assert common._suppressed_notification_types.get() == frozenset({"pb"})

    def test_empty_suppression_mutes_nothing(self, common):
        with common.suppress_notifications():
            assert common._suppressed_notification_types.get() == frozenset()

    def test_nested_blocks_restore_the_outer_set(self, common):
        with common.suppress_notifications("pb"):
            with common.suppress_notifications("dm_pb"):
                assert common._suppressed_notification_types.get() == frozenset({"dm_pb"})
            assert common._suppressed_notification_types.get() == frozenset({"pb"})
        assert common._suppressed_notification_types.get() == frozenset()

    def test_mute_does_not_leak_across_concurrent_tasks(self, common):
        """The ContextVar reason for existing: a sibling task must stay live."""

        async def scenario():
            seen = {}

            async def muted():
                with common.suppress_notifications("pb"):
                    await asyncio.sleep(0)
                    seen["muted"] = common._suppressed_notification_types.get()

            async def live():
                await asyncio.sleep(0)
                seen["live"] = common._suppressed_notification_types.get()

            await asyncio.gather(muted(), live())
            return seen

        seen = asyncio.run(scenario())
        assert seen["muted"] == frozenset({"pb"})
        assert seen["live"] == frozenset()
