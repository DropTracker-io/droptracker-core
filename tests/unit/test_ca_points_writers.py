"""What each writer of a player's combat achievement total hands the rule.

The rule itself — which reading wins — is test_ca_points.py. This pins the two
callers: a combat achievement completion offers the game's own total, for
plugin traffic on the main game only, dated by when the server accepted it and
committed before anything can yield the event loop; the account sync offers
what its merged bits are worth.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

# The real module conftest registered — patch.object on it reaches the callers'
# ``from db.ca_points import ...``; a dotted patch("db.ca_points...") would
# land on the stubbed ``db`` package's mock attribute instead.
CA_POINTS = sys.modules["db.ca_points"]

PLAYER_ID = 4137


class _FakePlayer:
    player_id = PLAYER_ID
    user_id = 99
    user = MagicMock()


def _session(order):
    """A session whose queries find nothing (a new completion) and whose
    commits are recorded in ``order``."""
    session = MagicMock()

    def _query(_model):
        q = MagicMock()
        q.filter.return_value = q
        q.filter_by.return_value = q
        q.order_by.return_value = q
        q.first.return_value = None
        q.all.return_value = []
        return q

    session.query.side_effect = _query
    session.commit.side_effect = lambda: order.append("commit")
    return session


def _completion(**overrides):
    payload = {
        "player_name": "MX PIaid", "acc_hash": "-537678130437333791",
        "task": "Brutus Novice", "tier": "Easy", "guid": "fresh-guid",
        "points": 1, "total_points": 1200, "p_v": "6.0.4",
    }
    payload.update(overrides)
    return payload


def _run(payload, world_type="main", record=None):
    import data.submissions.ca as ca

    order = []
    session = _session(order)
    if record is None:
        record = MagicMock(side_effect=lambda *a, **k: order.append("record") or True)
    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(ca, name, **kw))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_player_by_name_then_auth",
          new=AsyncMock(return_value=(_FakePlayer(), True, True)))
        p("ensure_can_create", new=AsyncMock(return_value=True))
        p("get_player_groups_with_global", new=MagicMock(return_value=[]))
        p("is_user_dm_enabled", new=MagicMock(return_value=False))
        p("award_points_to_player", new=MagicMock())
        p("create_notification", new=AsyncMock())
        stack.enter_context(patch.object(CA_POINTS, "record_ca_points", new=record))
        asyncio.run(ca.ca_processor(payload, external_session=session, world_type=world_type))
    return record, order, session


class TestCompletionsRecordTheGameTotal:
    def test_a_plugin_completion_offers_the_game_total(self):
        record, _, session = _run(_completion())
        record.assert_called_once()
        args = record.call_args.args
        assert args[:4] == (session, PLAYER_ID, 1200, "game")
        assert isinstance(args[4], datetime)

    def test_it_is_committed_before_anything_can_yield(self):
        # The row lock lasts until the commit; the consumer's six workers
        # share one event loop, so an await in between could stall them all.
        _, order, _ = _run(_completion())
        assert order[order.index("record") + 1] == "commit"

    def test_it_is_dated_by_when_the_server_accepted_it_however_old(self):
        # A replayed day-old submission must sort as a day old, or it would
        # lower a newer total. The row-dating helper's 6-hour cap must not
        # apply here.
        accepted = (datetime.now() - timedelta(days=1)).replace(microsecond=0)
        record, _, _ = _run(_completion(_received_at=accepted.isoformat()))
        assert record.call_args.args[4] == accepted

    # Each negative case also asserts the commit happened: the processor got
    # as far as recording and chose not to, rather than bailing out earlier.

    def test_a_league_completion_is_not_the_main_account_s_total(self):
        record, order, _ = _run(_completion(), world_type="seasonal")
        record.assert_not_called()
        assert "commit" in order

    def test_a_manual_submission_has_no_total_to_offer(self):
        # webhook.py sends 0: a web or Discord submission has no varbit.
        record, order, _ = _run(_completion(total_points=0))
        record.assert_not_called()
        assert "commit" in order

    def test_a_manual_submission_is_never_the_game_s_word(self):
        record, order, _ = _run(_completion(intake_source="manual"))
        record.assert_not_called()
        assert "commit" in order

    def test_a_broken_session_reaches_the_consumer_s_retry(self):
        # Swallowing it would leave the completion to fail at the commit as a
        # non-retryable PendingRollbackError (the 2026-08-30 dead-lettering).
        boom = MagicMock(side_effect=OperationalError("UPDATE", {}, Exception("2013")))
        with pytest.raises(OperationalError):
            _run(_completion(), record=boom)

    def test_a_logic_fault_does_not_cost_the_completion(self):
        _, order, _ = _run(_completion(), record=MagicMock(side_effect=ValueError("x")))
        assert "commit" in order


class TestSyncRecordsWhatItsBitsAreWorth:
    REGISTRY = [
        {"varp": 3116, "bit": 0, "tier": "Easy"},
        {"varp": 3116, "bit": 1, "tier": "Elite"},
        {"varp": 3117, "bit": 0, "tier": "Grandmaster"},
    ]

    @pytest.fixture
    def route(self):
        from api.routes import state_sync as route

        return route

    def _session_with(self, row):
        session = MagicMock()
        q = MagicMock()
        q.filter.return_value = q
        q.first.return_value = row
        session.query.return_value = q
        return session

    def _patched(self, registry):
        order = []
        record = MagicMock(side_effect=lambda *a, **k: order.append("record") or True)
        stack = ExitStack()
        stack.enter_context(patch.object(CA_POINTS, "record_ca_points", new=record))
        stack.enter_context(patch.object(CA_POINTS, "load_task_registry",
                                         new=MagicMock(return_value=registry)))
        return stack, record, order

    def test_offers_the_count_of_the_merged_bits(self, route):
        # Stored bits the client did not re-read still count: an older
        # manifest reads fewer varps.
        row = SimpleNamespace(varps='{"3117":1}', tasks_completed=1, completed_tasks=None)
        session = self._session_with(row)
        stack, record, _ = self._patched(self.REGISTRY)
        with stack:
            route._upsert_combat_achievements(session, PLAYER_ID, {3116: 0b11})
        args = record.call_args.args
        assert args[:4] == (session, PLAYER_ID, 1 + 4 + 6, "sync")
        assert isinstance(args[4], datetime)

    def test_flushes_first_so_a_new_row_exists_for_the_update(self, route):
        session = self._session_with(None)
        stack, record, order = self._patched(self.REGISTRY)
        session.flush.side_effect = lambda: order.append("flush")
        with stack:
            route._upsert_combat_achievements(session, PLAYER_ID, {3116: 1})
        assert order == ["flush", "record"]

    def test_a_points_only_row_gains_its_bits(self, route):
        # Created by a completion before the player ever synced.
        row = SimpleNamespace(varps=None, tasks_completed=None, completed_tasks=None)
        session = self._session_with(row)
        stack, record, _ = self._patched(self.REGISTRY)
        with stack:
            route._upsert_combat_achievements(session, PLAYER_ID, {3116: 1})
        assert row.varps == '{"3116":1}'
        assert row.tasks_completed == 1
        assert record.call_args.args[2] == 1

    def test_without_a_registry_nothing_is_offered(self, route):
        # "Cannot count" must not be stored as a total of zero.
        session = self._session_with(None)
        stack, record, _ = self._patched([])
        with stack:
            route._upsert_combat_achievements(session, PLAYER_ID, {3116: 1})
        record.assert_not_called()

    def test_a_snapshot_without_varps_offers_nothing(self, route):
        session = self._session_with(None)
        stack, record, _ = self._patched(self.REGISTRY)
        with stack:
            route._upsert_combat_achievements(session, PLAYER_ID, {}, completed_tasks=[5])
        record.assert_not_called()


class TestReceivedAtLagCap:
    def test_the_default_still_caps_for_dating_rows(self):
        from data.submissions.common import received_at

        old = datetime.now() - timedelta(days=2)
        assert received_at({"_received_at": old.isoformat()}) > old + timedelta(days=1)

    def test_no_cap_believes_a_stamp_of_any_age(self):
        from data.submissions.common import received_at

        old = (datetime.now() - timedelta(days=2)).replace(microsecond=0)
        assert received_at({"_received_at": old.isoformat()}, max_lag=None) == old

    def test_no_cap_still_rejects_a_future_stamp(self):
        from data.submissions.common import received_at

        future = datetime.now() + timedelta(hours=1)
        assert received_at({"_received_at": future.isoformat()}, max_lag=None) < future
