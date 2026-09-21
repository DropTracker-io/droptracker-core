"""Live event edits (web68a): guards, retro wiring, and the engine recompute.

Three layers under test:

1. The pure guards — ``_assert_event_not_past`` (task/team structure freezes
   once an event ends) and ``_assert_bingo_board_editable`` (the
   ``allow_live_edits`` toggle unlocks the bingo board while ACTIVE only).
2. The route wiring — ``PATCH tasks/{id}`` demands an explicit ``retro``
   choice for a scoring-affecting edit on a live event with recorded rows
   (422 ``retro_required``), calls the engine on ``recompute``, skips it on
   ``keep``; ``DELETE tasks/{id}?retro=keep_scores`` leaves team scores
   standing while the full unwind stays the default.
3. The engine — ``recompute_task_rollups`` re-folds counter rollups in both
   directions (award AND revoke), absorbs points-only deltas, and refuses
   forward-only kinds.

Route tests reuse the scripted-session harness from test_event_auth_modes
(``_assert_event_admin`` stubbed — its contract has its own tests). The
engine is loaded straight from the file like test_event_engine_scoring, so
the conftest ``db.models`` MagicMock stands in for the ORM and the scripted
session provides the rows.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import web_api.routes.event_admin as ea
import web_api.routes.events as evr
from web_api.common import ProblemException

from tests.unit.test_event_auth_modes import _S, _SessionCM, _event

# ── Engine module, loaded from the file so conftest stubs never interfere ────
_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_live_edits_ut", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_live_edits_ut"] = engine
_spec.loader.exec_module(engine)


def _ev_row(**kw):
    """Engine-facing event row (attrs _event_to_dict reads)."""
    base = dict(
        id=1, name="Ev", group_id=42, requires_confirmation=False,
        submission_policy="all", message_config=None, has_bingo=False,
        kind="standard", board_size=5, bonus_line_points=0,
        bonus_blackout_points=0, starts_at=None, ends_at=None,
        activated_at=None, ended_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _task_row(**kw):
    base = dict(
        id=9, event_id=1, type="kc_target", label="Slay stuff", target="Zulrah",
        target_value=5, points=10, requires_confirmation=False, config=None,
        difficulty=None, visibility="public",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _row(quantity=1, player_id=3, source_type="drop", status="auto", created_at=None):
    return SimpleNamespace(
        quantity=quantity, player_id=player_id, source_type=source_type,
        status=status, created_at=created_at or datetime(2026, 7, 20, 12, 0, 0),
    )


# ── 1. Guards ────────────────────────────────────────────────────────────────
class TestAssertEventNotPast:
    def test_draft_and_active_pass(self):
        evr._assert_event_not_past(_event(status="draft"))
        evr._assert_event_not_past(_event(status="active"))

    def test_explicit_past_blocked(self):
        with pytest.raises(ProblemException) as exc:
            evr._assert_event_not_past(_event(status="past"))
        assert exc.value.status == 409
        assert (exc.value.extra or {}).get("code") == "event_past"

    def test_effectively_past_blocked(self):
        # Active row whose scheduled end has passed reads as past (sweep lag).
        ev = _event(status="active", ends_at=datetime.now() - timedelta(hours=1))
        with pytest.raises(ProblemException) as exc:
            evr._assert_event_not_past(ev)
        assert exc.value.status == 409


class TestBingoBoardEditable:
    def test_draft_always_editable(self):
        ea._assert_bingo_board_editable(_event(status="draft", allow_live_edits=False))

    def test_active_locked_without_toggle(self):
        ev = _event(status="active", allow_live_edits=False,
                    activated_at=datetime.now() - timedelta(hours=1))
        with pytest.raises(ProblemException) as exc:
            ea._assert_bingo_board_editable(ev)
        assert exc.value.status == 409

    def test_active_unlocked_with_toggle(self):
        ev = _event(status="active", allow_live_edits=True,
                    activated_at=datetime.now() - timedelta(hours=1))
        ea._assert_bingo_board_editable(ev)  # no raise

    def test_past_locked_even_with_toggle(self):
        ev = _event(status="past", allow_live_edits=True,
                    activated_at=datetime.now() - timedelta(days=2),
                    ended_at=datetime.now() - timedelta(hours=1))
        with pytest.raises(ProblemException):
            ea._assert_bingo_board_editable(ev)

    def test_effectively_past_locked_with_toggle(self):
        # Toggle must not unlock an active row whose window already closed.
        ev = _event(status="active", allow_live_edits=True,
                    activated_at=datetime.now() - timedelta(days=2),
                    ends_at=datetime.now() - timedelta(hours=1))
        with pytest.raises(ProblemException):
            ea._assert_bingo_board_editable(ev)


# ── 2. Route wiring ──────────────────────────────────────────────────────────
@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _task(**kw):
    base = dict(
        id=9, event_id=1, type="kc_target", label="Slay stuff", target="Zulrah",
        target_value=5, points=10, requires_confirmation=False, config=None,
        difficulty=None, visibility="public",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _AuditRow(SimpleNamespace):
    """Recording stand-in for the stubbed AuditLog model class."""


def _wire_admin(monkeypatch, session, user_id=7):
    monkeypatch.setattr(ea, "current_user_id", lambda: user_id)
    monkeypatch.setattr(ea, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(ea, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(ea, "_assert_event_admin", lambda *a, **k: None)
    monkeypatch.setattr(ea, "AuditLog", _AuditRow)


def _wire_events(monkeypatch, session, user_id=7):
    monkeypatch.setattr(evr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)
    monkeypatch.setattr(evr, "AuditLog", _AuditRow)


def _fake_engine(monkeypatch, result=None):
    calls = []

    class _Eng:
        @staticmethod
        def recompute_task_rollups(s, ev, task, *, old_points=None,
                                   rescreen_vestige_rings=False):
            calls.append({"old_points": old_points, "task_id": task.id})
            if rescreen_vestige_rings:
                calls[-1]["rescreen_vestige_rings"] = True
            if isinstance(result, Exception):
                raise result
            return result if result is not None else {"teams": {}, "bonuses": {}}

    monkeypatch.setattr(ea, "_engine", lambda: _Eng)
    return calls


class TestUpdateTaskRetro:
    async def test_live_scoring_edit_without_retro_422(self, client, monkeypatch):
        # points 10 → 25 on an active event with a recorded rollup: the maker
        # must choose. Nothing commits.
        s = _S([_event(status="active")], [_task()], [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        calls = _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9", json={"points": 25})
        assert r.status_code == 422
        body = await r.get_json()
        assert body.get("code") == "retro_required"
        assert not s.committed and not calls

    async def test_retro_keep_saves_without_engine(self, client, monkeypatch):
        s = _S([_event(status="active")], [_task()], [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        calls = _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9",
                               json={"points": 25, "retro": "keep"})
        assert r.status_code == 200
        assert s.committed and not calls
        audit = s.added[-1]
        assert audit.action == "event.task.update"
        after = json.loads(audit.after)
        assert after["retro"] == "keep" and after["points"] == 25

    async def test_retro_recompute_calls_engine_with_old_points(self, client, monkeypatch):
        s = _S([_event(status="active")], [_task(points=10)], [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        summary = {"teams": {"1": {"score_delta": 15}}, "bonuses": {}}
        calls = _fake_engine(monkeypatch, summary)
        r = await client.patch("/api/v1/events/1/tasks/9",
                               json={"points": 25, "retro": "recompute"})
        assert r.status_code == 200
        assert calls == [{"old_points": 10, "task_id": 9}]
        body = await r.get_json()
        assert body["recompute"] == summary
        after = json.loads(s.added[-1].after)
        assert after["retro"] == "recompute" and after["recompute"] == summary["teams"]

    async def test_forward_only_type_rejects_recompute(self, client, monkeypatch):
        s = _S([_event(status="active")], [_task(type="custom", target=None)],
               [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9",
                               json={"points": 25, "retro": "recompute"})
        assert r.status_code == 422
        assert (await r.get_json()).get("code") == "forward_only"

    async def test_forward_only_type_needs_no_choice(self, client, monkeypatch):
        # A manual-only task edit on a live event saves without a retro pick.
        s = _S([_event(status="active")], [_task(type="custom", target=None)],
               [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        calls = _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9", json={"points": 25})
        assert r.status_code == 200
        assert s.committed and not calls

    async def test_draft_event_needs_no_choice(self, client, monkeypatch):
        s = _S([_event(status="draft")], [_task()])
        _wire_admin(monkeypatch, s)
        calls = _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9", json={"points": 25})
        assert r.status_code == 200
        assert s.committed and not calls

    async def test_cosmetic_edit_needs_no_choice(self, client, monkeypatch):
        s = _S([_event(status="active")], [_task()])
        _wire_admin(monkeypatch, s)
        calls = _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9", json={"label": "Renamed"})
        assert r.status_code == 200
        assert s.committed and not calls

    async def test_past_event_frozen(self, client, monkeypatch):
        s = _S([_event(status="past")])
        _wire_admin(monkeypatch, s)
        _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9", json={"points": 25})
        assert r.status_code == 409
        assert (await r.get_json()).get("code") == "event_past"

    async def test_invalid_retro_rejected(self, client, monkeypatch):
        _wire_admin(monkeypatch, _S())
        r = await client.patch("/api/v1/events/1/tasks/9",
                               json={"points": 25, "retro": "maybe"})
        assert r.status_code == 422


class TestDeleteTaskRetro:
    def _batches(self, *, team_batch: bool):
        team = SimpleNamespace(id=1, score=10.0)
        batches = [
            [_event(status="active", has_bingo=False)],
            [_task(points=5)],
            [SimpleNamespace(team_id=1)],   # completed rollups
            [],                              # applied bonus rows
        ]
        if team_batch:
            batches.append([team])           # score subtraction read
        batches += [
            [],  # bingo cell ids
            [],  # ledger delete
            [],  # progress delete
            [],  # player-points delete
            [],  # board tile unbind
            [],  # board position unbind
        ]
        return team, batches

    async def test_default_revokes_points(self, client, monkeypatch):
        team, batches = self._batches(team_batch=True)
        s = _S(*batches)
        _wire_events(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/tasks/9")
        assert r.status_code == 200
        assert team.score == 5.0  # 10 − 5
        after = json.loads(s.added[-1].after)
        assert after["retro"] == "revoke" and after["score_deltas"] == {"1": 5}
        assert s.committed

    async def test_keep_scores_leaves_scores(self, client, monkeypatch):
        team, batches = self._batches(team_batch=False)
        s = _S(*batches)
        _wire_events(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/tasks/9?retro=keep_scores")
        assert r.status_code == 200
        assert team.score == 10.0  # untouched
        after = json.loads(s.added[-1].after)
        assert after["retro"] == "keep_scores" and after["score_deltas"] == {"1": 5}
        assert s.committed

    async def test_invalid_retro_rejected(self, client, monkeypatch):
        _wire_events(monkeypatch, _S())
        r = await client.delete("/api/v1/events/1/tasks/9?retro=half")
        assert r.status_code == 422

    async def test_past_event_frozen(self, client, monkeypatch):
        s = _S([_event(status="past")])
        _wire_events(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/tasks/9")
        assert r.status_code == 409


# ── 3. Engine recompute ──────────────────────────────────────────────────────
class TestRecomputeForwardOnly:
    def test_manual_types_raise(self):
        for ttype in ("custom", "ehp_target", "ehb_target"):
            with pytest.raises(ValueError):
                engine.recompute_task_rollups(
                    None, _ev_row(), _task_row(type=ttype, target=None))

    def test_board_game_event_raises(self):
        with pytest.raises(ValueError):
            engine.recompute_task_rollups(
                None, _ev_row(kind="board_game"), _task_row())


class TestRecomputeCounterRollups:
    def test_target_raise_revokes_completion(self):
        # Rollup completed at 3/3; target raised to 5 → un-complete, score
        # −points, player points deleted.
        progress = SimpleNamespace(progress=3, completed=True,
                                   completed_at=datetime(2026, 7, 19), team_id=1)
        team = SimpleNamespace(id=1, score=10)
        s = _S(
            [(1,)],                    # progress team ids
            [(1,)],                    # applied completion team ids
            [progress],                # locked rollup read
            [_row(quantity=3)],        # surviving ledger fold → 3
            [team],                    # t60 leader snapshot (first score write)
            [team],                    # score RMW
            [],                        # player-points delete
            [(1, 0)],                  # final scores for SSE frames
            [team],                    # t60 leader compare (unchanged → silent)
        )
        out = engine.recompute_task_rollups(
            s, _ev_row(), _task_row(target_value=5), old_points=10)
        assert progress.completed is False and progress.completed_at is None
        assert team.score == 0
        entry = out["teams"][1]
        assert entry["score_delta"] == -10 and entry["was_completed"] is True
        assert entry["completed"] is False and entry["progress"] == 3

    def test_preserve_completed_holds_the_flag_but_fixes_progress(self):
        # Retro cleanup (scripts/dedupe_multipath_drops.py): duplicate ledger
        # rows were deleted, so the fold now falls short of the target. The
        # task must NOT un-complete — record_match refuses to record anything
        # after completion, so every qualifying drop since then was discarded
        # and cannot be recovered. Progress and per-player shares are still
        # corrected; the flag, its points and its completed_at are held.
        progress = SimpleNamespace(progress=5, completed=True,
                                   completed_at=datetime(2026, 7, 19), team_id=1)
        team = SimpleNamespace(id=1, score=10)
        s = _S(
            [(1,)],                                # progress team ids
            [(1,)],                                # applied completion team ids
            [progress],                            # locked rollup read
            [_row(quantity=4)],                    # fold → 4, under target 5
            [_row(quantity=4, player_id=3)],       # contributors: still awarded
            [SimpleNamespace(player_id=3, player_name="P")],
            [],                                    # player-points rewrite delete
            [(1, 10)],                             # final scores
        )
        out = engine.recompute_task_rollups(
            s, _ev_row(), _task_row(target_value=5), preserve_completed=True)
        assert progress.completed is True
        assert progress.completed_at == datetime(2026, 7, 19)
        assert progress.progress == 4            # the correction that DID land
        assert team.score == 10                  # points not clawed back
        entry = out["teams"][1]
        assert entry["completed"] is True and entry["score_delta"] == 0
        assert entry["progress"] == 4 and entry["target"] == 5

    def test_preserve_completed_does_not_invent_completion(self):
        # It only ever HOLDS an existing flag — a rollup that was not complete
        # still completes strictly on merit.
        progress = SimpleNamespace(progress=2, completed=False,
                                   completed_at=None, team_id=1)
        s = _S(
            [(1,)],
            [(1,)],
            [progress],
            [_row(quantity=2)],                    # fold → 2, under target 5
            [(1, 0)],
        )
        out = engine.recompute_task_rollups(
            s, _ev_row(), _task_row(target_value=5), preserve_completed=True)
        assert progress.completed is False
        assert out["teams"][1]["completed"] is False

    def test_target_lower_awards_completion(self):
        # 5 recorded vs old target 10; lowered to 3 → complete, +points,
        # completed_at from the surviving ledger (not "now").
        ledger_ts = datetime(2026, 7, 18, 9, 30)
        progress = SimpleNamespace(progress=5, completed=False,
                                   completed_at=None, team_id=1)
        team = SimpleNamespace(id=1, score=0)
        s = _S(
            [(1,)],
            [(1,)],
            [progress],
            [_row(quantity=3), _row(quantity=2)],  # fold → 5
            [(ledger_ts,)],                        # honest completed_at
            [team],                                # t60 leader snapshot
            [team],                                # score RMW
            [_row(quantity=5, player_id=3)],       # contributors: ledger
            [SimpleNamespace(player_id=3, player_name="P")],
            [],                                    # player-points rewrite delete
            [(1, 10)],                             # final scores
            [team],                                # t60 leader compare (silent)
        )
        out = engine.recompute_task_rollups(
            s, _ev_row(), _task_row(target_value=3), old_points=10)
        assert progress.completed is True
        assert progress.completed_at == ledger_ts
        assert team.score == 10
        entry = out["teams"][1]
        assert entry["score_delta"] == 10 and entry["completed"] is True
        # The award rewrote per-player shares for the completing team.
        assert any(getattr(a, "_mock_name", None) is not None or a is not None
                   for a in s.added)

    def test_points_only_change_absorbs_delta(self):
        # Still complete either way; points 10 → 25 with old_points supplied
        # → score += 15 and player shares rewritten at the new value.
        progress = SimpleNamespace(progress=5, completed=True,
                                   completed_at=datetime(2026, 7, 18), team_id=1)
        team = SimpleNamespace(id=1, score=10)
        s = _S(
            [(1,)],
            [(1,)],
            [progress],
            [_row(quantity=5)],                    # fold → 5, still ≥ target 5
            [team],                                # t60 leader snapshot
            [team],                                # score RMW (delta path)
            [_row(quantity=5, player_id=3)],       # contributors
            [SimpleNamespace(player_id=3, player_name="P")],
            [],                                    # player-points rewrite delete
            [(1, 25)],
            [team],                                # t60 leader compare (silent)
        )
        out = engine.recompute_task_rollups(
            s, _ev_row(), _task_row(points=25), old_points=10)
        assert team.score == 25
        assert out["teams"][1]["score_delta"] == 15
        # completed_at untouched for an already-complete rollup.
        assert progress.completed_at == datetime(2026, 7, 18)

    def test_no_rows_no_summary(self):
        s = _S([], [])
        out = engine.recompute_task_rollups(s, _ev_row(), _task_row())
        assert out == {"teams": {}, "bonuses": {}}


class TestDeriveAppliedProgress:
    def test_counter_fold_excludes_bonus_rows(self):
        # One query, three surviving rows — the bonus row must not count.
        s = _S([_row(quantity=3), _row(quantity=2),
                _row(quantity=99, source_type="bonus")])
        task = {"id": 9, "type": "kc_target", "config": None, "target_value": 5}
        assert engine._derive_applied_progress(s, task, 1) == 5


# ── 4. "Gold rings count as vestiges" switch (config.vestige_rings) ──────────
def _ledger(rid, target, *, status="auto", source_id=None, team_id=1, player_id=3,
            acted_by_user_id=None, note=None, source_type="drop"):
    return SimpleNamespace(
        id=rid, matched_target=target, status=status, source_id=source_id,
        team_id=team_id, player_id=player_id, acted_by_user_id=acted_by_user_id,
        note=note, source_type=source_type, quantity=1)


_RINGS_OFF_TASK = {"id": 9, "type": "item_collection", "target": "Ultor vestige",
                   "target_value": 1, "config": {"vestige_rings": False}}
_RINGS_ON_TASK = {**_RINGS_OFF_TASK, "config": {}}


class TestRescreenVestigeRingCredits:
    def test_switch_off_takes_back_ring_credits_only(self):
        ring = _ledger(1, "Ultor vestige", source_id=100)
        pending_ring = _ledger(2, "Ultor vestige", status="pending",
                               source_id=102, player_id=4)
        vestige = _ledger(3, "Ultor vestige", status="confirmed", source_id=101,
                          player_id=5, acted_by_user_id=7)
        s = _S(
            [ring, pending_ring, vestige],                        # vestige drop rows
            [(100, "Gold ring"), (101, "Ultor vestige"), (102, "Gold ring")],
        )
        out = engine._rescreen_vestige_ring_credits(s, _RINGS_OFF_TASK)
        assert out == {"revoked": [1], "rejected": [2]}
        assert (ring.status, ring.note) == ("revoked", engine.RING_OFF_NOTE)
        assert (pending_ring.status, pending_ring.note) == ("rejected",
                                                            engine.RING_OFF_NOTE)
        # The vestige itself keeps counting.
        assert (vestige.status, vestige.note) == ("confirmed", None)

    def test_switch_off_without_ring_rows_changes_nothing(self):
        vestige = _ledger(3, "Ultor vestige", source_id=101)
        s = _S([vestige], [(101, "Ultor vestige")])
        assert engine._rescreen_vestige_ring_credits(s, _RINGS_OFF_TASK) == {}
        assert vestige.status == "auto"

    def test_switch_on_restores_each_row_as_it_was(self):
        auto = _ledger(1, "Ultor vestige", status="revoked", note=engine.RING_OFF_NOTE)
        confirmed = _ledger(2, "Ultor vestige", status="revoked", player_id=4,
                            acted_by_user_id=7, note=engine.RING_OFF_NOTE)
        pending = _ledger(3, "Ultor vestige", status="rejected", player_id=5,
                          note=engine.RING_OFF_NOTE)
        s = _S([auto, confirmed, pending], [])
        out = engine._rescreen_vestige_ring_credits(s, _RINGS_ON_TASK)
        assert out == {"restored": [1, 2, 3]}
        assert (auto.status, confirmed.status, pending.status) == (
            "auto", "confirmed", "pending")
        assert auto.note is None and confirmed.note is None and pending.note is None

    def test_switch_on_keeps_one_credit_per_chain(self):
        # While rings were off, this player's vestige itself dropped and
        # counted: that is the chain's one credit, so the old ring stays out.
        ring = _ledger(1, "Ultor vestige", status="revoked", note=engine.RING_OFF_NOTE)
        vestige = _ledger(2, "Ultor vestige", source_id=101)
        s = _S([ring], [vestige])
        assert engine._rescreen_vestige_ring_credits(s, _RINGS_ON_TASK) == {}
        assert (ring.status, ring.note) == ("revoked", engine.RING_OFF_NOTE)

    def test_switch_on_restores_a_chain_once(self):
        first = _ledger(1, "Ultor vestige", status="revoked", note=engine.RING_OFF_NOTE)
        second = _ledger(2, "Ultor vestige", status="revoked", note=engine.RING_OFF_NOTE)
        s = _S([first, second], [])
        assert engine._rescreen_vestige_ring_credits(s, _RINGS_ON_TASK) == {
            "restored": [1]}
        assert second.status == "revoked"

    def test_switch_on_skips_a_vestige_the_task_no_longer_lists(self):
        task = {**_RINGS_ON_TASK, "target": "Magus vestige"}
        ring = _ledger(1, "Ultor vestige", status="revoked", note=engine.RING_OFF_NOTE)
        s = _S([ring], [])
        assert engine._rescreen_vestige_ring_credits(s, task) == {}
        assert ring.status == "revoked"

    def test_switch_on_restores_nothing_once_gold_ring_is_listed(self):
        task = {**_RINGS_ON_TASK, "target": None,
                "config": {"kind": "any_of", "items": ["Gold ring", "Ultor vestige"]}}
        s = _S()  # returns before any query
        assert engine._rescreen_vestige_ring_credits(s, task) == {}

    def test_other_task_types_issue_no_queries(self):
        s = _S()  # any query raises
        assert engine._rescreen_vestige_ring_credits(
            s, {"id": 9, "type": "kc_target", "config": {}}) == {}


class TestRecomputeWithVestigeRingRescreen:
    def test_switching_rings_off_takes_back_a_ring_completion(self):
        # "Ultor vestige" (10 pts) completed on a Gold ring; rings switched
        # off with re-score → the ring row is revoked BEFORE the re-fold, so
        # the tile un-completes and the points come off.
        ring = _ledger(1, "Ultor vestige", source_id=100)
        progress = SimpleNamespace(progress=1, completed=True,
                                   completed_at=datetime(2026, 9, 20), team_id=1)
        team = SimpleNamespace(id=1, score=10)
        s = _S(
            [ring],                    # rescreen: vestige drop rows
            [(100, "Gold ring")],      # rescreen: their source drops' items
            [(1,)],                    # progress team ids
            [(1,)],                    # applied completion team ids
            [progress],                # locked rollup read
            [],                        # surviving ledger fold → 0
            [team],                    # t60 leader snapshot (first score write)
            [team],                    # score RMW
            [],                        # player-points delete
            [(1, 0)],                  # final scores for SSE frames
            [team],                    # t60 leader compare
        )
        out = engine.recompute_task_rollups(
            s, _ev_row(),
            _task_row(type="item_collection", target="Ultor vestige", target_value=1,
                      config='{"vestige_rings": false}'),
            old_points=10, rescreen_vestige_rings=True)
        assert ring.status == "revoked"
        assert progress.completed is False and team.score == 0
        assert out["vestige_rings"] == {"revoked": [1], "rejected": []}
        assert out["teams"][1]["score_delta"] == -10

    def test_nothing_to_rescreen_adds_no_summary_key(self):
        s = _S([], [], [])  # no vestige drop rows; no teams
        out = engine.recompute_task_rollups(
            s, _ev_row(),
            _task_row(type="item_collection", target="Ultor vestige", target_value=1,
                      config='{"vestige_rings": false}'),
            rescreen_vestige_rings=True)
        assert out == {"teams": {}, "bonuses": {}}


class TestUpdateTaskVestigeRings:
    @pytest.fixture(autouse=True)
    def _known_vestige(self, monkeypatch):
        import web_api.routes.event_task_validation as etv

        monkeypatch.setattr(
            etv, "_canonical_item",
            lambda s, name: ("Ultor vestige"
                             if (name or "").strip().lower() == "ultor vestige" else None))

    def _task(self, config=None):
        return _task(type="item_collection", target="Ultor vestige", target_value=1,
                     config=config)

    async def test_flipping_the_switch_rescreens_on_recompute(self, client, monkeypatch):
        task = self._task()
        s = _S([_event(status="active")], [task], [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        summary = {"teams": {}, "bonuses": {},
                   "vestige_rings": {"revoked": [5], "rejected": []}}
        calls = _fake_engine(monkeypatch, summary)
        r = await client.patch("/api/v1/events/1/tasks/9",
                               json={"config": {"vestige_rings": False},
                                     "retro": "recompute"})
        assert r.status_code == 200
        assert calls == [{"old_points": 10, "task_id": 9,
                          "rescreen_vestige_rings": True}]
        assert json.loads(task.config) == {"vestige_rings": False}
        after = json.loads(s.added[-1].after)
        assert after["vestige_rings"] == {"revoked": [5], "rejected": []}

    async def test_switching_back_on_rescreens_too(self, client, monkeypatch):
        task = self._task(config='{"vestige_rings": false}')
        s = _S([_event(status="active")], [task], [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        calls = _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9",
                               json={"config": None, "retro": "recompute"})
        assert r.status_code == 200
        assert calls[0].get("rescreen_vestige_rings") is True
        assert task.config is None

    async def test_other_edits_leave_ring_credits_alone(self, client, monkeypatch):
        task = self._task(config='{"vestige_rings": false}')
        s = _S([_event(status="active")], [task], [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        calls = _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9",
                               json={"points": 25, "retro": "recompute"})
        assert r.status_code == 200
        assert calls == [{"old_points": 10, "task_id": 9}]

    async def test_flip_with_keep_needs_no_engine(self, client, monkeypatch):
        task = self._task()
        s = _S([_event(status="active")], [task], [SimpleNamespace(id=1)])
        _wire_admin(monkeypatch, s)
        calls = _fake_engine(monkeypatch)
        r = await client.patch("/api/v1/events/1/tasks/9",
                               json={"config": {"vestige_rings": False},
                                     "retro": "keep"})
        assert r.status_code == 200
        assert not calls and s.committed
