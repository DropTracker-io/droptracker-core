"""Manual clog / PB / CA / pet submissions get a real review (2026-11-23).

Under a clan's ``confirm`` policy these used to be silently suppressed — no
card, nothing to approve (a pet received on mobile simply never posted). Now
their group notification is written ``held`` with a review card, approving
releases it, and (like a held drop) a held entry earns no group points.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import data.submissions.manual_policy as mp


def _load_service(name):
    """`services` is a stub package in unit tests: load the real module by path."""
    import importlib.util
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        f"_{name}_under_test", os.path.join(here, "..", "..", "services", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _load_service("review_buttons_core")


# --------------------------------------------------------------------------- #
# Policy split
# --------------------------------------------------------------------------- #
class TestNotificationSplit:
    def _split(self, monkeypatch, moderation):
        monkeypatch.setattr(mp, "manual_moderation_for_player",
                            lambda *a, **k: moderation)
        return mp.manual_notification_split(object(), object(), [190, 191, 192])

    def test_confirm_holds_and_block_excludes(self, monkeypatch):
        excluded, held = self._split(monkeypatch, {
            190: ("pending", "confirm"),
            191: ("excluded", "block"),
            192: ("excluded", "authorized_only"),
        })
        assert held == {190}
        assert excluded == {191, 192}

    def test_trusted_submitter_neither(self, monkeypatch):
        assert self._split(monkeypatch, {}) == (set(), set())


class TestHoldCard:
    def test_summary_names_the_thing(self):
        assert mp.hold_summary("pet", {"pet_name": "Yami"}) == "a pet: **Yami**"
        assert mp.hold_summary("clog", {"item_name": "Unsired"}) == \
            "a collection log slot: **Unsired**"
        assert mp.hold_summary("ca", {"task_name": "Perfect Zulrah"}) == \
            "a combat achievement: **Perfect Zulrah**"
        assert mp.hold_summary("pb", {"npc_name": "Zulrah", "time_formatted": "0:52.20"}) == \
            "a personal best: **Zulrah (0:52.20)**"
        assert mp.hold_summary("pet", {}) == "a pet"

    def test_buttons_parse_back(self):
        buttons = mp.hold_review_buttons(190, 4242)
        ids = [b["custom_id"] for b in buttons if "custom_id" in b]
        assert [core.parse_custom_id(i) for i in ids] == [
            {"kind": "hold", "action": "approve", "group_id": 190, "notification_id": 4242},
            {"kind": "hold", "action": "reject", "group_id": 190, "notification_id": 4242},
        ]
        assert core.parse_custom_id(core.hold_custom_id("undo", 190, 4242))["action"] == "undo"

    def test_undo_restores_hold_buttons_not_drop_buttons(self):
        edit = core.undone_message({"components": []}, "took it back", 190, 4242, kind="hold")
        ids = [c["custom_id"] for row in edit["components"] for c in row.get("components", [])
               if isinstance(c, dict) and "custom_id" in c]
        assert ids and all(i.startswith("dtrv:n:") for i in ids)


# --------------------------------------------------------------------------- #
# State machine (services/manual_hold_review)
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, row):
        self.row = row

    def filter(self, *a, **k):
        return self

    def with_for_update(self):
        return self

    def first(self):
        return self.row


class _S:
    def __init__(self, row):
        self.row = row

    def query(self, *a):
        return _Q(self.row)

    def flush(self):
        pass


mhr = _load_service("manual_hold_review")


def _row(status):
    return SimpleNamespace(id=4242, group_id=190, notification_type="pet",
                           status=status, error_message=None)


class TestHoldStateMachine:
    def test_approve_releases_to_the_sender(self):
        row = _row("held")
        out = mhr.approve(_S(row), 4242, 190)
        assert row.status == "pending"  # the core bot drains 'pending'
        assert (out["previous_status"], out["status"]) == ("pending", "approved")

    def test_reject_never_posts(self):
        row = _row("held")
        mhr.reject(_S(row), 4242, 190)
        assert row.status == "rejected"

    def test_a_decided_entry_cannot_be_decided_again(self):
        with pytest.raises(mhr.HoldReviewError):
            mhr.approve(_S(_row("rejected")), 4242, 190)
        with pytest.raises(mhr.HoldReviewError):
            mhr.reject(_S(_row("pending")), 4242, 190)

    def test_undo_a_rejection_or_an_unsent_approval(self):
        for status in ("rejected", "pending"):
            row = _row(status)
            mhr.undo(_S(row), 4242, 190)
            assert row.status == "held"

    def test_undo_after_posting_is_refused(self):
        for status in ("processing", "sent"):
            with pytest.raises(mhr.HoldReviewError) as e:
                mhr.undo(_S(_row(status)), 4242, 190)
            assert e.value.title == "Already posted"

    def test_missing_entry_is_404(self):
        with pytest.raises(mhr.HoldReviewError) as e:
            mhr.approve(_S(None), 4242, 190)
        assert e.value.status == 404


# --------------------------------------------------------------------------- #
# Pet processor end to end: held group -> held notification, no points
# --------------------------------------------------------------------------- #
class _FakePlayer:
    player_id = 5753041
    user_id = 99
    user = MagicMock()
    player_name = "Mio Ookami"
    wom_id = 1


def _session(player):
    session = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.filter_by.return_value = q
        q.order_by.return_value = q
        q.first.return_value = player if model is type(player) else None
        q.all.return_value = []
        return q

    session.query.side_effect = _query
    return session


def _drive_pet(held_groups, excluded_groups=()):
    import data.submissions.pet as pet

    player = _FakePlayer()
    session = _session(player)
    notify = AsyncMock()
    points = AsyncMock(return_value={"receiver_points_awarded": 5, "receiver_current_points": 5,
                                     "total_points_awarded": 5, "awarded_members": []})
    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(pet, name, **kw))
        p("_new_to_collection_log", new=MagicMock(return_value=False))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_player_by_name_then_auth", new=AsyncMock(return_value=(player, True, True)))
        p("ensure_item_by_name", new=AsyncMock(return_value=MagicMock(item_id=30888)))
        p("ensure_npc_id_for_player", new=AsyncMock(return_value=(14176, "Yama")))
        p("ensure_can_create", new=AsyncMock(return_value=True))
        p("get_player_groups_with_global", new=MagicMock(return_value=[
            MagicMock(group_id=g, group_name=f"G{g}") for g in (190, 191, 192)]))
        p("screenshot_required", new=AsyncMock(return_value=False))
        p("create_notification", new=notify)
        p("is_user_dm_enabled", new=MagicMock(return_value=False))
        p("get_config_prefix", new=MagicMock(return_value=""))
        stack.enter_context(patch("data.submissions.manual_policy.manual_notification_split",
                                  return_value=(set(excluded_groups), set(held_groups))))
        stack.enter_context(
            patch("data.submissions.common.check_group_point_system_active", return_value=True))
        stack.enter_context(patch("data.submissions.point_awards.check_and_award_points", new=points))
        stack.enter_context(patch("data.submissions.pet_points.plan_live_pet",
                                  return_value=SimpleNamespace(award=True, duplicate=False,
                                                               duplicate_award=0)))
        stack.enter_context(patch("utils.group_config.get_effective", return_value="1"))
        stack.enter_context(patch("services.realtime.publish_feed_pet", create=True))
        stack.enter_context(patch.dict(sys.modules, {
            "services.event_engine": SimpleNamespace(queue_submission=MagicMock())}))
        asyncio.run(pet.pet_processor(
            {"player_name": "Mio Ookami", "acc_hash": "-1", "pet_name": "Yami",
             "guid": "hold-guid", "source": "Yama", "intake_source": "manual"},
            external_session=session))
    sent = {c.args[3]: c.kwargs.get("held") for c in notify.await_args_list if c.args[0] == "pet"}
    pointed = {c.args[1] for c in points.await_args_list}
    return sent, pointed


class TestPetProcessorHold:
    def test_held_group_gets_a_held_notification_and_no_points(self):
        sent, pointed = _drive_pet(held_groups={190}, excluded_groups={191})
        assert sent == {190: True, 192: False}   # 191 excluded outright
        assert 190 not in pointed and 191 not in pointed
        assert 192 in pointed

    def test_nothing_held_behaves_as_before(self):
        sent, pointed = _drive_pet(held_groups=set())
        assert sent == {190: False, 191: False, 192: False}
        assert pointed == {190, 191, 192}


# --------------------------------------------------------------------------- #
# Late proof reaches the source rows and held announcements
# --------------------------------------------------------------------------- #
class TestProofPropagation:
    def test_fills_drop_and_held_announcement(self):
        from data.submissions import manual_proof as proof

        executed = []

        class _Res:
            def __init__(self, rows=None, rowcount=1):
                self._rows, self.rowcount = rows or [], rowcount

            def fetchall(self):
                return self._rows

        class _Sess:
            def execute(self, stmt, params):
                sql = str(stmt)
                executed.append((sql, params))
                if sql.startswith("SELECT source_type"):
                    return _Res([("drop", 900, "g-1#p0"), ("pet", None, "g-2")])
                return _Res()

        out = proof.propagate_proof_to_sources(_Sess(), [1, 2], [5], "https://x/p.png")
        assert out == {"sources": 1, "held": 1}
        drop_sql = [p for s, p in executed if s.startswith("UPDATE drops")]
        assert drop_sql and drop_sql[0]["sid"] == 900
        held = [p for s, p in executed if s.startswith("UPDATE notification_queue")]
        # The path suffix is stripped back to the submission guid.
        assert held and held[0]["guids"] == ["g-1", "g-2"]
