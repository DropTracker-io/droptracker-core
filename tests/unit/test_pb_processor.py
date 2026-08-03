"""pb_processor update/sync/notify semantics (ticket #361).

The plugin reports two times on every kill message: the measured kill time
(current_ms) and, on non-PB kills, the game's standing personal best (pb_ms).
Live submissions can carry a kill time measured on a DIFFERENT metric than the
stored row (ToB "total completion time" vs the POH adventure-log's in-raid
time), so rows went stale forever and genuine PBs were silently discarded.

The contract these tests pin down:

- A kill faster than BOTH the stored row and the reported best is a real PB:
  row updated to the kill time, notification created.
- Any kill whose reported best (pb_ms) is faster than the stored row syncs the
  row down SILENTLY — no notification, no points, no ticker — even when the
  kill itself was slow or untimed. PBs self-heal on every kill.
- A kill that beats the stale stored row but NOT the game's reported best is a
  sync, not an announcement (the game did not call it a PB).
- Nothing faster than the stored row → row untouched.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import db


class _FakePlayer:
    player_id = 5751994
    user_id = 2010
    user = MagicMock()
    player_name = "Ashey"


def _session(rows=None):
    """Session double: queries routed by model, writes are no-ops."""
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
    session.flush.return_value = None
    return session


def _existing_row(personal_best):
    row = MagicMock()
    row.personal_best = personal_best
    row.kill_time = personal_best
    row.new_pb = True
    row.image_url = ""
    return row


def _payload(current_ms, pb_ms, is_pb):
    return {
        "player_name": "Ashey",
        "acc_hash": "-5493473578450257792",
        "npc_name": "Theatre of Blood",
        "current_time_ms": current_ms,
        "personal_best_ms": pb_ms,
        "team_size": "4",
        "is_new_pb": "true" if is_pb else "false",
        "guid": "test-guid-1",
        "p_v": "5.4.0",
    }


def _run(payload, row=None):
    import data.submissions.pb as pb

    player = _FakePlayer()
    rows = {_FakePlayer: player}
    if row is not None:
        rows[db.PersonalBestEntry] = row
    session = _session(rows)
    create_notification = AsyncMock()
    db.PersonalBestEntry.reset_mock()

    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(pb, name, **kw))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_can_create", new=AsyncMock(return_value=True))
        p("ensure_npc_id_for_player",
          new=AsyncMock(return_value=(13699, "Theatre of Blood")))
        p("ensure_player_by_name_then_auth",
          new=AsyncMock(return_value=(player, True, True)))
        p("get_player_groups_with_global",
          new=MagicMock(return_value=[MagicMock(group_id=2, group_name="G")]))
        p("screenshot_required", new=AsyncMock(return_value=False))
        p("create_notification", new=create_notification)
        p("is_user_dm_enabled", new=MagicMock(return_value=False))
        p("get_config_prefix", new=MagicMock(return_value=""))
        p("is_truthy_config", new=MagicMock(return_value=True))
        stack.enter_context(patch("utils.pb_blocklist.is_blocked", return_value=False))
        stack.enter_context(patch("utils.group_config.get_bulk",
                                  return_value={(2, "notify_pbs"): "true"}))
        stack.enter_context(patch("utils.pb_rank.pb_board_rank", return_value=None))
        stack.enter_context(
            patch("data.submissions.common.check_group_point_system_active",
                  return_value=False)
        )
        asyncio.run(pb.pb_processor(payload, external_session=session))
    return session, create_notification


class TestGenuinePb:
    def test_kill_faster_than_stored_and_no_reported_best_updates_and_notifies(self):
        """New-PB messages omit the "Personal best:" segment (pb_ms=0)."""
        row = _existing_row(904800)  # stored 15:04.8
        _, notify = _run(_payload(current_ms=888000, pb_ms=0, is_pb=True), row=row)
        assert row.personal_best == 888000
        assert row.kill_time == 888000
        assert row.new_pb is True
        assert notify.await_count > 0, "a genuine PB must announce"

    def test_first_row_genuine_pb_creates_and_notifies(self):
        _, notify = _run(_payload(current_ms=888000, pb_ms=0, is_pb=True), row=None)
        kwargs = db.PersonalBestEntry.call_args.kwargs
        assert kwargs["personal_best"] == 888000
        assert kwargs["new_pb"] is True
        assert notify.await_count > 0


class TestSilentSync:
    def test_slow_kill_with_faster_reported_best_syncs_without_notifying(self):
        """Ticket #361: ToB total-time kill vs POH in-raid stored row."""
        row = _existing_row(921600)  # stored 15:21.6
        _, notify = _run(_payload(current_ms=982800, pb_ms=855600, is_pb=False), row=row)
        assert row.personal_best == 855600
        assert row.kill_time == 855600
        assert row.new_pb is False
        assert notify.await_count == 0, "a sync must stay silent"

    def test_kill_beating_stale_row_but_not_reported_best_is_a_sync_not_a_pb(self):
        """The game did not call it a PB, so neither do we — but the row heals."""
        row = _existing_row(904800)
        _, notify = _run(_payload(current_ms=900000, pb_ms=890000, is_pb=False), row=row)
        assert row.personal_best == 890000
        assert row.new_pb is False
        assert notify.await_count == 0

    def test_untimed_kill_with_faster_reported_best_syncs(self):
        """current_ms == 0 ("N/A") must not block the reported-best heal."""
        row = _existing_row(921600)
        _, notify = _run(_payload(current_ms=0, pb_ms=855600, is_pb=False), row=row)
        assert row.personal_best == 855600
        assert notify.await_count == 0

    def test_first_row_from_slow_kill_stores_reported_best_without_notifying(self):
        """Poisoned is_pb=true (room-PB merge) with a contradicting reported
        best must create the row from the best time but never announce."""
        _, notify = _run(_payload(current_ms=982800, pb_ms=855600, is_pb=True), row=None)
        kwargs = db.PersonalBestEntry.call_args.kwargs
        assert kwargs["personal_best"] == 855600
        assert kwargs["new_pb"] is False
        assert notify.await_count == 0


class TestNoOp:
    def test_nothing_faster_leaves_row_untouched(self):
        row = _existing_row(900000)
        _, notify = _run(_payload(current_ms=982800, pb_ms=921600, is_pb=False), row=row)
        assert row.personal_best == 900000
        assert row.kill_time == 900000
        assert notify.await_count == 0

    def test_untimed_kill_with_slower_reported_best_is_a_no_op(self):
        row = _existing_row(900000)
        _, notify = _run(_payload(current_ms=0, pb_ms=921600, is_pb=True), row=row)
        assert row.personal_best == 900000
        assert notify.await_count == 0
