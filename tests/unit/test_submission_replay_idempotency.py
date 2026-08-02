"""A replayed submission must be a no-op, never a second row or a second ping.

Context (incident 2026-08-02). The intake API was down for 2h31m and the
recovery replayed failed submissions hours later. Those replays were written
AGAIN rather than recognised as already-recorded, because the dedup lookup in
``ensure_can_create`` only looked back one hour. That lookup is now unbounded
(covered in test_common.py), and migration web84a puts a UNIQUE index on
``unique_id`` underneath it for the four small tables as a backstop.

This module covers the backstop's behaviour: when the UNIQUE index fires, the
processor must treat it as a SUCCESSFUL no-op — return quietly, create no
Discord notification — rather than raise and dead-letter the entry. Getting
that wrong turns a harmless replay into a poison-pill queue entry.

Every candidate design for surviving the next outage (a spooler, a failover
origin, draining a backlog) works by replaying traffic late, so "late replay is
a no-op" is the property the whole plan rests on.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError


def _integrity_error():
    return IntegrityError("INSERT ...", {}, Exception("Duplicate entry for key 'uq_unique_id'"))


def _replay_session(flush_error=None, rows=None):
    """A session whose nested-savepoint flush fails the way a UNIQUE index does.

    Mirrors the real shape: ``with session.begin_nested(): add(); flush()``
    raises IntegrityError, exactly as MariaDB reports a duplicate key.

    Queries are ROUTED BY MODEL (``rows``) rather than all returning the same
    thing. The processors re-fetch the player off the same session
    (``session.query(type(player))``, clog.py:144, ca.py:130), so a blanket
    ``first() -> None`` aborts them at that re-fetch and the test passes
    without ever reaching the insert it is supposed to be exercising. Models
    absent from ``rows`` return None, which is what puts each processor on its
    "create a new entry" branch.

    Deliberately a configured MagicMock rather than a MagicMock SUBCLASS —
    pytest's traceback recursion check walks the object on failure and a
    subclass generates child mocks forever, hanging the run instead of
    reporting the failure.
    """
    rows = rows or {}
    session = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.filter_by.return_value = q
        q.order_by.return_value = q
        q.first.return_value = rows.get(model)
        q.all.return_value = []
        return q

    session.query.side_effect = _query
    session.flush.side_effect = flush_error or _integrity_error()
    return session


class _FakePlayer:
    player_id = 4137
    user_id = 99
    user = MagicMock()


def _common_patches(stack, module, create_notification):
    """Patch every boundary the processors share.

    Not every processor imports every helper (ca.py has no NPC lookup, for
    one), so each is patched only where it exists.
    """
    shared = [
        ("ensure_can_create", AsyncMock(return_value=True)),
        ("ensure_npc_id_for_player", AsyncMock(return_value=(13943, "unknown"))),
        ("get_player_groups_with_global", MagicMock(return_value=[MagicMock(group_id=2)])),
        ("screenshot_required", AsyncMock(return_value=False)),
        ("create_notification", create_notification),
        ("is_user_dm_enabled", MagicMock(return_value=False)),
        ("get_config_prefix", MagicMock(return_value="")),
        ("award_points_to_player", MagicMock()),
    ]
    for name, value in shared:
        if hasattr(module, name):
            stack.enter_context(patch.object(module, name, new=value))
    stack.enter_context(
        patch("data.submissions.common.check_group_point_system_active", return_value=False)
    )
    stack.enter_context(patch("utils.group_config.get", return_value="true"))
    stack.enter_context(patch("utils.group_config.is_truthy", return_value=True))


def _run(module, coro_name, payload, extra=()):
    """Drive a processor against a session that raises IntegrityError on flush."""
    import asyncio

    player = _FakePlayer()
    session = _replay_session(rows={_FakePlayer: player})
    create_notification = AsyncMock()
    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(module, name, **kw))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_player_by_name_then_auth", new=AsyncMock(return_value=(player, True, True)))
        _common_patches(stack, module, create_notification)
        for name, value in extra:
            p(name, new=value)
        asyncio.run(getattr(module, coro_name)(payload, external_session=session))
    # Guard against a vacuous pass: the processor must actually have reached
    # the guarded insert, not bailed out earlier on an unmocked boundary.
    assert session.begin_nested.called, (
        f"{coro_name} never reached the savepoint-guarded insert"
    )
    assert session.add.called, f"{coro_name} never staged a row"
    return session, create_notification


class TestReplayIsANoOp:
    """A duplicate-key failure is a success, not an error."""

    def test_clog_replay_does_not_raise_or_notify(self):
        import data.submissions.clog as clog

        item = MagicMock(item_id=2978)
        session, notify = _run(
            clog, "clog_processor",
            {"player_name": "MX PIaid", "acc_hash": "-537678130437333791",
             "item": "Twisted bow", "guid": "replayed-guid", "source": "unknown",
             "kc": 0, "p_v": "5.3.0"},
            extra=[("ensure_item_by_name", AsyncMock(return_value=item))],
        )
        assert notify.await_count == 0, "a replay must not re-announce to Discord"

    def test_ca_replay_does_not_raise_or_notify(self):
        import data.submissions.ca as ca

        session, notify = _run(
            ca, "ca_processor",
            {"player_name": "MX PIaid", "acc_hash": "-537678130437333791",
             "task": "Brutus Novice", "tier": "Easy", "guid": "replayed-guid",
             "points": 10, "total_points": 1200, "p_v": "5.3.0"},
        )
        assert notify.await_count == 0, "a replay must not re-announce to Discord"

    def test_pet_replay_does_not_raise_or_notify(self):
        import data.submissions.pet as pet

        session, notify = _run(
            pet, "pet_processor",
            {"player_name": "MX PIaid", "acc_hash": "-537678130437333791",
             "pet_name": "Baby mole", "guid": "replayed-guid", "source": "Giant Mole",
             "p_v": "5.3.0"},
            extra=[("ensure_item_by_name", AsyncMock(return_value=MagicMock(item_id=12646)))],
        )
        assert notify.await_count == 0, "a replay must not re-announce to Discord"

    def test_pb_replay_does_not_raise_or_notify(self):
        import data.submissions.pb as pb

        session, notify = _run(
            pb, "pb_processor",
            {"player_name": "MX PIaid", "acc_hash": "-537678130437333791",
             "npc_name": "Brutus", "current_time_ms": 36000, "personal_best_ms": 36000,
             "team_size": 1, "is_new_pb": "true", "guid": "replayed-guid",
             "p_v": "5.3.0"},
        )
        assert notify.await_count == 0, "a replay must not re-announce to Discord"

    def test_integrity_error_is_not_swallowed_as_a_generic_exception(self):
        """The handler must be specific to IntegrityError.

        A genuine DB fault has to keep failing loudly so the entry retries or
        dead-letters — silently treating every error as "already recorded"
        would lose real submissions, which is the failure this whole change
        exists to prevent.
        """
        import data.submissions.clog as clog
        import asyncio

        player = _FakePlayer()
        session = _replay_session(flush_error=RuntimeError("connection lost"),
                                  rows={_FakePlayer: player})
        item = MagicMock(item_id=2978)
        with ExitStack() as stack:
            p = lambda name, **kw: stack.enter_context(patch.object(clog, name, **kw))
            p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
            p("ensure_item_by_name", new=AsyncMock(return_value=item))
            p("ensure_player_by_name_then_auth",
              new=AsyncMock(return_value=(player, True, True)))
            _common_patches(stack, clog, AsyncMock())
            with pytest.raises(RuntimeError):
                asyncio.run(clog.clog_processor(
                    {"player_name": "MX PIaid", "acc_hash": "-537678130437333791",
                     "item": "Twisted bow", "guid": "some-guid", "source": "unknown",
                     "kc": 0, "p_v": "5.3.0"},
                    external_session=session))
