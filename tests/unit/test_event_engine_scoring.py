"""Unit tests for the scoring-side helpers of services/event_engine.py:

- ``_current_leader(strict=True)`` — a shared top score has NO leader, so a
  team that merely ties can never fire a lead-change embed (the id-order
  tiebreak used to crown them).
- ``_award_contribution_points`` — task points split across contributors by
  net quantity share (floats), mutating each contributor's ``points_share``.
- ``_row_advances_progress`` — the record gate that drops ledger rows whose
  matched item is already satisfied.

Loaded the same way as test_event_engine_matcher: straight from the file so
the conftest sys.modules stubs (db.models is a MagicMock) never interfere —
the fake sessions below stand in for the ORM.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_scoring_ut", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_scoring_ut"] = engine
_spec.loader.exec_module(engine)


class _Q:
    """Chainable query fake returning pre-sorted rows."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a):
        return self

    def with_for_update(self, *a, **k):
        return self

    def limit(self, n):
        return _Q(self._rows[:n])

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def distinct(self):
        return self

    def count(self):
        return len(self._rows)

    def delete(self, synchronize_session=False):
        n = len(self._rows)
        self._rows.clear()
        return n


class _Session:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.added = []

    def query(self, *a):
        return _Q(self.rows)

    def add(self, obj):
        self.added.append(obj)


def _team(tid, score):
    return SimpleNamespace(id=tid, score=score)


class TestCurrentLeaderStrict:
    def test_tie_has_no_strict_leader(self):
        # The reported bug: 20-20 after a catch-up completion crowned the
        # lower-id team and fired "New leader".
        s = _Session([_team(1, 20), _team(2, 20)])
        assert engine._current_leader(s, 99, strict=True) is None

    def test_tie_still_reports_loose_leader(self):
        s = _Session([_team(1, 20), _team(2, 20)])
        assert engine._current_leader(s, 99) == (1, 20)

    def test_distinct_scores_report_top(self):
        s = _Session([_team(2, 25), _team(1, 20)])
        assert engine._current_leader(s, 99, strict=True) == (2, 25)

    def test_single_team_is_strict_leader(self):
        s = _Session([_team(7, 0)])
        assert engine._current_leader(s, 99, strict=True) == (7, 0)

    def test_no_teams(self):
        s = _Session([])
        assert engine._current_leader(s, 99, strict=True) is None


class TestAwardContributionPoints:
    EVENT = {"id": 5}
    TASK = {"id": 9, "points": 5}

    def test_split_by_net_share(self):
        contributors = [
            {"player_id": 1, "player_name": "A", "quantity": 3},
            {"player_id": 2, "player_name": "B", "quantity": 1},
        ]
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, contributors, 5)
        assert contributors[0]["points_share"] == 3.75
        assert contributors[1]["points_share"] == 1.25
        assert len(s.added) == 2

    def test_even_split_halves(self):
        contributors = [
            {"player_id": 1, "quantity": 2},
            {"player_id": 2, "quantity": 2},
        ]
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, contributors, 5)
        assert [c["points_share"] for c in contributors] == [2.5, 2.5]

    def test_zero_points_awards_nothing(self):
        contributors = [{"player_id": 1, "quantity": 2}]
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, contributors, 0)
        assert s.added == [] and "points_share" not in contributors[0]

    def test_zero_total_quantity_awards_nothing(self):
        contributors = [{"player_id": 1, "quantity": 0}]
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, contributors, 5)
        assert s.added == []

    def test_no_contributors(self):
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, [], 5)
        assert s.added == []


class _Row:
    """EventCompletion stand-in for the rollup queries (needs .id for the
    include-dedupe check)."""

    def __init__(self, matched_target=None, quantity=1, source_type="drop", rid=1,
                 note=None, player_id=None):
        self.id = rid
        self.matched_target = matched_target
        self.quantity = quantity
        self.source_type = source_type
        self.note = note
        self.player_id = player_id


class TestRowAdvancesProgress:
    ALL_OF = {
        "id": 3, "type": "item_collection", "target_value": 2,
        "config": {"kind": "all_of", "items": [
            {"item_name": "Bones"}, {"item_name": "Coins"},
        ]},
    }

    def test_duplicate_item_is_dead_weight(self):
        s = _Session([_Row("Bones", rid=10)])
        candidate = _Row("Bones", rid=None)
        assert engine._row_advances_progress(s, self.ALL_OF, 4, candidate) is False

    def test_new_item_advances(self):
        s = _Session([_Row("Bones", rid=10)])
        candidate = _Row("Coins", rid=None)
        assert engine._row_advances_progress(s, self.ALL_OF, 4, candidate) is True

    def test_plain_count_tasks_always_advance(self):
        task = {"id": 3, "type": "item_collection", "target_value": 5, "config": {}}
        s = _Session([])
        assert engine._row_advances_progress(s, task, 4, _Row("Bones", rid=None)) is True

    # any_path metric paths: the percent rollup floors, so a 1-kill row on a
    # 5,000-KC path never moves the integer — it must still record (progress
    # re-folds from ledger rows; a dropped row is credit lost forever).
    GWD_OR = {
        "id": 3, "type": "item_collection", "target_value": 100,
        "config": {"kind": "any_path", "paths": [
            {"groups": [{"mode": "any_of", "need": 1, "items": ["Armadyl hilt"]}]},
            {"metric": "kc", "npcs": ["Kree'arra"], "need": 500},
        ]},
    }

    def test_metric_row_advances_below_its_paths_need(self):
        s = _Session([_Row(None, quantity=200, note="path:1", rid=10)])
        candidate = _Row(None, quantity=1, note="path:1", rid=None)
        assert engine._row_advances_progress(s, self.GWD_OR, 4, candidate) is True

    def test_metric_row_is_dead_weight_once_need_met(self):
        s = _Session([_Row(None, quantity=500, note="path:1", rid=10)])
        candidate = _Row(None, quantity=1, note="path:1", rid=None)
        assert engine._row_advances_progress(s, self.GWD_OR, 4, candidate) is False

    def test_metric_row_for_unknown_path_is_dropped(self):
        s = _Session([])
        candidate = _Row(None, quantity=1, note="path:9", rid=None)
        assert engine._row_advances_progress(s, self.GWD_OR, 4, candidate) is False


class TestPbEffectiveThreshold:
    """effective_threshold — whole_team pb tasks scale to the roster; every
    other task passes through the pure completion_threshold. The fake session
    returns its row list for the member query (each row = one member)."""

    WHOLE = {"id": 9, "type": "pb_target", "target": "Zulrah", "target_value": 70,
             "config": {"mode": "whole_team"}}

    def test_roster_size_is_the_threshold(self):
        s = _Session([_Row(rid=1), _Row(rid=2), _Row(rid=3)])
        assert engine.effective_threshold(s, self.WHOLE, 4) == 3

    def test_empty_roster_floors_to_one(self):
        s = _Session([])
        assert engine.effective_threshold(s, self.WHOLE, 4) == 1

    def test_teamless_context_falls_back_pure(self):
        assert engine.effective_threshold(_Session([]), self.WHOLE, None) == 1

    def test_other_tasks_pass_through(self):
        times = {"id": 9, "type": "pb_target", "target_value": 70,
                 "config": {"mode": "times", "need": 5}}
        assert engine.effective_threshold(_Session([_Row()]), times, 4) == 5
        kc = {"type": "kc_target", "target_value": 50}
        assert engine.effective_threshold(_Session([]), kc, 4) == 50


class TestPbUniqueAdvance:
    """_row_advances_progress on unique-player pb rollups: a player who
    already beat the time is dead weight on a repeat kill."""

    UNIQ = {"id": 8, "type": "pb_target", "target": "Zulrah", "target_value": 70,
            "config": {"mode": "unique_players", "need": 3}}

    def test_repeat_player_is_dead_weight(self):
        s = _Session([_Row(rid=10, player_id=5)])
        assert engine._row_advances_progress(
            s, self.UNIQ, 4, _Row(rid=None, player_id=5)) is False

    def test_new_player_advances(self):
        s = _Session([_Row(rid=10, player_id=5)])
        assert engine._row_advances_progress(
            s, self.UNIQ, 4, _Row(rid=None, player_id=6)) is True

    def test_times_mode_always_advances(self):
        times = {"id": 8, "type": "pb_target", "target": "Zulrah",
                 "target_value": 70, "config": {"mode": "times", "need": 5}}
        s = _Session([_Row(rid=10, player_id=5)])
        assert engine._row_advances_progress(
            s, times, 4, _Row(rid=None, player_id=5)) is True


class TestVestigeChainDedupe:
    """_dedupe_vestige_chain — one player's ring/ring/vestige/clog chain
    credits a (task, team, vestige) once, no time window. The fake session
    stands for the SQL-side task/team/player/status filters; the name and
    source_type checks are python-side."""

    TASK = {"id": 7, "type": "item_collection", "target_value": 2,
            "config": {"kind": "any_of",
                       "items": ["Ultor vestige", "Venator vestige"]}}

    def test_second_ring_credit_is_blocked(self):
        s = _Session([_Row("Ultor vestige", rid=10)])
        assert engine._dedupe_vestige_chain(
            s, self.TASK, 4, 9, "drop", "Ultor vestige") is False

    def test_vestige_clog_weeks_after_ring_is_blocked(self):
        # Outside the 10-minute drop↔clog echo window — the chain rule has none.
        s = _Session([_Row("Ultor vestige", rid=10)])
        assert engine._dedupe_vestige_chain(
            s, self.TASK, 4, 9, "clog", "ultor  VESTIGE") is False

    def test_a_different_vestige_still_credits(self):
        s = _Session([_Row("Ultor vestige", rid=10)])
        assert engine._dedupe_vestige_chain(
            s, self.TASK, 4, 9, "drop", "Venator vestige") is True

    def test_manual_awards_do_not_block(self):
        s = _Session([_Row("Ultor vestige", source_type="manual", rid=10)])
        assert engine._dedupe_vestige_chain(
            s, self.TASK, 4, 9, "drop", "Ultor vestige") is True

    def test_non_vestige_items_unaffected(self):
        s = _Session([_Row("Bones", rid=10)])
        assert engine._dedupe_vestige_chain(
            s, self.TASK, 4, 9, "drop", "Bones") is True

    def test_first_credit_passes(self):
        s = _Session([])
        assert engine._dedupe_vestige_chain(
            s, self.TASK, 4, 9, "drop", "Ultor vestige") is True


class TestClogEchoDedupe:
    """_dedupe_clog_echo — one physical acquisition must credit an
    item_collection task once, whichever of its drop/clog submissions lands
    first (the fake session stands for the SQL-side task/team/player/kind/
    window filters; the name check is python-side)."""

    TASK = {"id": 5, "type": "item_collection", "config": {}}

    @pytest.fixture(autouse=True)
    def _real_created_at_column(self, monkeypatch):
        # The stubbed EventCompletion's MagicMock attrs return NotImplemented
        # for ordering ops; the window filter needs created_at >= cutoff to
        # evaluate (the fake query ignores the result anyway).
        import datetime as _dt

        import db.models as dbm
        monkeypatch.setattr(dbm.EventCompletion, "created_at",
                            _dt.datetime(2000, 1, 1), raising=False)

    def test_clog_echo_of_recent_drop_is_skipped(self):
        s = _Session([_Row("Armadyl chainskirt", quantity=3, source_type="drop", rid=1)])
        assert engine._dedupe_clog_echo(
            s, self.TASK, 4, 7, "clog", "Armadyl chainskirt", 3) is None

    def test_clog_for_unrelated_item_passes(self):
        s = _Session([_Row("Armadyl helmet", quantity=2, source_type="drop", rid=1)])
        assert engine._dedupe_clog_echo(
            s, self.TASK, 4, 7, "clog", "Armadyl chainskirt", 3) == 3

    def test_drop_after_clog_credits_the_remainder(self):
        # First-ever stackable: the 1-unit clog echo landed first; the real
        # 287-quantity drop still credits the remaining 286.
        s = _Session([_Row("Zulrah's scales", quantity=1, source_type="clog", rid=1)])
        assert engine._dedupe_clog_echo(
            s, self.TASK, 4, 7, "drop", "Zulrah's scales", 287) == 286

    def test_drop_fully_pre_credited_is_skipped(self):
        s = _Session([_Row("Armadyl chainskirt", quantity=3, source_type="clog", rid=1)])
        assert engine._dedupe_clog_echo(
            s, self.TASK, 4, 7, "drop", "Armadyl chainskirt", 3) is None

    def test_name_comparison_is_normalized(self):
        s = _Session([_Row("armadyl  CHAINSKIRT", quantity=3, source_type="drop", rid=1)])
        assert engine._dedupe_clog_echo(
            s, self.TASK, 4, 7, "clog", "Armadyl chainskirt", 3) is None

    def test_non_item_tasks_pass_through(self):
        s = _Session([_Row("Zulrah", quantity=1, source_type="drop", rid=1)])
        task = {"id": 5, "type": "kc_target", "config": {}}
        assert engine._dedupe_clog_echo(s, task, 4, 7, "drop", "Zulrah", 1) == 1

    def test_other_kinds_and_missing_target_pass_through(self):
        s = _Session([_Row("X", quantity=5, source_type="drop", rid=1)])
        assert engine._dedupe_clog_echo(s, self.TASK, 4, 7, "manual", "X", 5) == 5
        assert engine._dedupe_clog_echo(s, self.TASK, 4, 7, "drop", None, 5) == 5


class _ModelSession:
    """Routes query(model) by the conftest-stubbed db.models attribute the
    engine imports lazily, so multi-table functions can run against fakes."""

    def __init__(self, tables):
        self.tables = tables
        self.added = []

    def query(self, model):
        return _Q(self.tables.get(model, []))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


class TestLineBonusCoalescing:
    """evaluate_bingo_bonuses — every line earned in one evaluation shares a
    single event_line notification (per-line messages rendered as
    indistinguishable 'completed a full line' duplicates)."""

    EVENT = {"id": 7, "has_bingo": True, "board_size": 3,
             "bonus_line_points": 10, "bonus_blackout_points": 25}

    def _tables(self, done_idxs):
        import db.models as dbm
        cells = [SimpleNamespace(id=i + 1, idx=i, task_id=99, label=f"C{i}")
                 for i in range(9)]
        done = [SimpleNamespace(cell_id=i + 1, team_id=4) for i in done_idxs]
        team = SimpleNamespace(id=4, name="Reds", score=0)
        return {
            dbm.EventBingoCell: cells,
            dbm.EventBingoCompletion: done,
            dbm.EventCompletion: [],
            dbm.EventTeam: [team],
        }, team

    def _run(self, monkeypatch, done_idxs):
        tables, team = self._tables(done_idxs)
        session = _ModelSession(tables)
        enqueued, frames = [], []
        monkeypatch.setattr(engine, "_enqueue_notification",
                            lambda s, t, e, p, d: enqueued.append((t, d)))
        monkeypatch.setattr(engine, "_publish",
                            lambda eid, frame: frames.append(frame))
        results = engine.evaluate_bingo_bonuses(
            session, self.EVENT, 4, trigger_task_id=99,
            player_id=7, player_name="Zed")
        return results, enqueued, frames, team, session

    def test_two_lines_earn_one_notification(self, monkeypatch):
        # Row 0 + column 0 of a 3×3 complete together (idxs 0,1,2 + 3,6).
        results, enqueued, frames, team, session = self._run(
            monkeypatch, [0, 1, 2, 3, 6])
        assert sorted(r["note"] for r in results) == ["line:c0", "line:r0"]
        assert len(session.added) == 2          # one ledger row per line
        assert len(frames) == 2                 # one SSE frame per line
        assert len(enqueued) == 1               # ONE Discord notification
        ntype, data = enqueued[0]
        assert ntype == "event_line"
        assert data["lines"] == ["line:c0", "line:r0"]
        assert data["line"] == "line:c0"        # legacy single-line key
        assert data["bonus_points"] == 20
        assert team.score == 20

    def test_single_line_still_carries_lines_list(self, monkeypatch):
        results, enqueued, _, team, _ = self._run(monkeypatch, [0, 1, 2])
        assert [r["note"] for r in results] == ["line:r0"]
        assert len(enqueued) == 1
        _, data = enqueued[0]
        assert data["lines"] == ["line:r0"] and data["bonus_points"] == 10
        assert team.score == 10

    def test_blackout_stays_its_own_message(self, monkeypatch):
        results, enqueued, _, team, _ = self._run(monkeypatch, range(9))
        types = [t for t, _ in enqueued]
        assert types.count("event_line") == 1
        assert types.count("event_blackout") == 1
        line_data = dict(enqueued)[u"event_line"]
        assert len(line_data["lines"]) == 8     # 3 rows + 3 cols + 2 diagonals
        assert line_data["bonus_points"] == 80
        blackout_data = dict(enqueued)["event_blackout"]
        assert blackout_data["bonus_points"] == 25
        assert team.score == 105


class TestPluginProgressStep:
    """10%-step gate for continuous-metric (xp/gp) plugin progress fan-out."""

    def test_crossing_a_decile_fires(self):
        # 900k -> 1.1M of 10M crosses the 10% boundary at 1M.
        assert engine._plugin_progress_step_crossed(900_000, 1_100_000, 10_000_000)

    def test_within_a_decile_is_silent(self):
        assert not engine._plugin_progress_step_crossed(1_100_000, 1_900_000, 10_000_000)

    def test_first_increment_from_zero_is_silent_until_ten_pct(self):
        assert not engine._plugin_progress_step_crossed(0, 999_999, 10_000_000)
        assert engine._plugin_progress_step_crossed(0, 1_000_000, 10_000_000)

    def test_multi_decile_jump_fires_once(self):
        assert engine._plugin_progress_step_crossed(0, 5_000_000, 10_000_000)

    def test_degenerate_threshold_always_fires(self):
        assert engine._plugin_progress_step_crossed(3, 4, 0)

    def test_only_continuous_types_are_stepped(self):
        assert set(engine.PLUGIN_PROGRESS_STEP_TASK_TYPES) == {"xp_target", "loot_value"}
