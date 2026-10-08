"""Repeatable event tasks (utils/task_repeat.py).

The report: on a standard event, "acquire 100k gp from Zulrah" could only be
done once per team — the first completion closed the task and every later
drop was discarded. A repeatable task keeps a running total instead; every
whole multiple of the target is another completion that pays the task's
points again, up to an optional cap.

Pinned here:

* the pure helpers (eligibility, cap, lap count, the "(×N)" label);
* the validator accepts the flag on count-style tasks only and bounds the cap;
* ``apply_ledger_row`` pays per lap (several at once for one big drop), keeps
  the task open until the cap, and only freezes / closes it at the cap;
* a repeat flag on a bingo task (copied from the library, say) is inert;
* ``revoke_ledger_row`` and ``recompute_task_rollups`` take back / re-price
  exactly the laps the ledger no longer supports, counting paid laps against
  the goal they were paid under;
* the "mark complete" projection and pending overlay mean "the next lap".

The engine is loaded straight from the file (test_event_engine_scoring's
pattern) and driven against a model-dispatched fake session.
"""

import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from utils import task_repeat as tr

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "_event_repeat_ut", os.path.join(_ROOT, "services", "event_engine.py"))
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_repeat_ut"] = engine
_spec.loader.exec_module(engine)


# ── Pure helpers ─────────────────────────────────────────────────────────────

class TestHelpers:
    def test_flag_off_is_single(self):
        assert tr.repeat_cap("loot_value", {}) == 1
        assert tr.repeat_cap("loot_value", {"repeatable": False}) == 1
        # Only a real boolean counts — a stray string never turns it on.
        assert tr.repeat_cap("loot_value", {"repeatable": "true"}) == 1

    def test_unlimited_and_capped(self):
        assert tr.repeat_cap("loot_value", {"repeatable": True}) is None
        assert tr.repeat_cap("loot_value", {"repeatable": True,
                                            "max_completions": 3}) == 3

    def test_accepts_json_string_config(self):
        raw = json.dumps({"repeatable": True, "max_completions": 4})
        assert tr.repeat_cap("kc_target", raw) == 4

    def test_only_standard_events_repeat(self):
        cfg = {"repeatable": True}
        assert tr.repeat_cap("loot_value", cfg, event_kind="standard") is None
        for kind in ("bingo", "board_game", "conquest", "loot_sweep", "sotw", "botw"):
            assert tr.repeat_cap("loot_value", cfg, event_kind=kind) == 1

    @pytest.mark.parametrize("ttype,cfg", [
        ("item_collection", {}),
        ("item_collection", {"kind": "any_of"}),
        ("item_collection", {"kind": "point_collection"}),
        ("kc_target", {}), ("xp_target", {}), ("loot_value", {"source_npcs": ["Zulrah"]}),
        ("pet_collection", {}), ("ca_target", {}), ("slayer_target", {}),
        ("pb_target", {"mode": "times", "need": 3}), ("pb_target", {}),
        ("custom", {}), ("ehp_target", {}), ("ehb_target", {}),
    ])
    def test_eligible(self, ttype, cfg):
        assert tr.eligible(ttype, cfg)

    @pytest.mark.parametrize("ttype,cfg", [
        ("item_collection", {"kind": "all_of"}),
        ("item_collection", {"kind": "assembly"}),
        ("item_collection", {"kind": "any_of_distinct"}),
        ("item_collection", {"kind": "groups"}),
        ("item_collection", {"kind": "any_path"}),
        ("pb_target", {"mode": "unique_players"}),
        ("pb_target", {"mode": "whole_team"}),
        ("skill_target", {}), ("loot_sweep", {}), ("competition", {}),
    ])
    def test_not_eligible(self, ttype, cfg):
        assert not tr.eligible(ttype, cfg)
        # …and the flag is inert on them even if it got stored somehow.
        assert tr.repeat_cap(ttype, {**cfg, "repeatable": True}) == 1

    def test_completion_count(self):
        assert tr.completion_count(0, 100, False, None) == 0
        assert tr.completion_count(99, 100, False, None) == 0
        assert tr.completion_count(100, 100, False, None) == 1
        assert tr.completion_count(350, 100, False, None) == 3
        assert tr.completion_count(350, 100, False, 2) == 2
        # Ordinary tasks follow the flag, whatever the total says.
        assert tr.completion_count(350, 100, True, 1) == 1
        assert tr.completion_count(350, 100, False, 1) == 0

    def test_is_maxed(self):
        assert tr.is_maxed(1, 1)
        assert not tr.is_maxed(0, 1)
        assert not tr.is_maxed(10_000, None)
        assert tr.is_maxed(3, 3) and not tr.is_maxed(2, 3)

    def test_cycle_progress(self):
        assert tr.cycle_progress(250, 100, 2, None) == 50
        assert tr.cycle_progress(250, 100, 2, 2) == 250  # capped: the total
        assert tr.cycle_progress(40, 100, 1, 1) == 40

    def test_suffix(self):
        assert tr.repeat_suffix(0) == ""
        assert tr.repeat_suffix(1) == ""
        assert tr.repeat_suffix(3) == " (×3)"

    def test_lap_threshold(self):
        assert tr.lap_threshold("loot_value", 100_000, None) == 100_000
        assert tr.lap_threshold("pb_target", None, {"mode": "times", "need": 4}) == 4
        assert tr.lap_threshold("custom", None, None) == 1

    def test_rollup_completions(self):
        cfg = {"repeatable": True}
        assert tr.rollup_completions("loot_value", 100, cfg, "standard", 250, False) == 2
        assert tr.rollup_completions("loot_value", 100, cfg, "bingo", 250, True) == 1
        assert tr.rollup_completions("loot_value", 100, None, "standard", 250, True) == 1


# ── Validation ───────────────────────────────────────────────────────────────

@pytest.fixture
def etv(monkeypatch):
    import web_api.routes.event_task_validation as module

    monkeypatch.setattr(module, "_canonical_item",
                        lambda s, name: {"boater": "Boater", "red boater": "Red boater"}
                        .get((name or "").strip().lower()))
    return module


def _problem():
    from web_api.common import ProblemException
    return ProblemException


class TestValidation:
    def test_single_item_keeps_flag_and_cap(self, etv):
        out = etv.validate_task_payload(None, {
            "type": "item_collection", "target": "boater", "target_value": 2,
            "config": {"repeatable": True, "max_completions": 5}})
        assert json.loads(out["config"]) == {"repeatable": True, "max_completions": 5}

    def test_unlimited_stores_no_cap(self, etv):
        out = etv.validate_task_payload(None, {
            "type": "item_collection",
            "config": {"kind": "any_of", "items": ["Boater", "Red boater"],
                       "repeatable": True}})
        cfg = json.loads(out["config"])
        assert cfg["repeatable"] is True and "max_completions" not in cfg

    def test_false_flag_is_dropped(self, etv):
        out = etv.validate_task_payload(None, {
            "type": "item_collection", "target": "boater",
            "config": {"repeatable": False}})
        assert out["config"] is None

    def test_set_tasks_cannot_repeat(self, etv):
        with pytest.raises(_problem()):
            etv.validate_task_payload(None, {
                "type": "item_collection",
                "config": {"kind": "all_of", "items": ["Boater", "Red boater"],
                           "repeatable": True}})

    def test_level_tasks_cannot_repeat(self, etv, monkeypatch):
        with pytest.raises(_problem()):
            etv.validate_task_payload(None, {
                "type": "skill_target", "target": "Attack", "target_value": 99,
                "config": {"repeatable": True}})

    @pytest.mark.parametrize("bad", [1, 0, -3, 10_001, 2.5, "3", True])
    def test_cap_bounds(self, etv, bad):
        with pytest.raises(_problem()):
            etv.validate_task_payload(None, {
                "type": "item_collection", "target": "boater",
                "config": {"repeatable": True, "max_completions": bad}})

    def test_non_boolean_flag_rejected(self, etv):
        with pytest.raises(_problem()):
            etv.validate_task_payload(None, {
                "type": "item_collection", "target": "boater",
                "config": {"repeatable": "yes"}})

    def test_custom_task_can_repeat(self, etv):
        out = etv.validate_task_payload(None, {
            "type": "custom", "target": "Screenshot", "target_value": 1,
            "config": {"repeatable": True}})
        assert json.loads(out["config"]) == {"repeatable": True}


# ── Fake ORM ─────────────────────────────────────────────────────────────────

class _Col:
    def __init__(self, model, name):
        self._model, self._name = model, name

    def __eq__(self, other):
        return None

    def __hash__(self):
        return id(self)

    def in_(self, *a):
        return None

    def is_(self, *a):
        return None

    def isnot(self, *a):
        return None

    def desc(self):
        return None

    def asc(self):
        return None


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Progress(_Row):
    pass


class _Team(_Row):
    pass


class _Ledger(_Row):
    pass


class _PlayerPoints(_Row):
    pass


class _Event(_Row):
    pass


class _Task(_Row):
    pass


for _cls, _names in ((_Progress, ("id", "task_id", "team_id", "event_id", "completed")),
                     (_Team, ("id", "event_id", "score")),
                     (_Ledger, ("id", "task_id", "team_id", "status", "created_at",
                                "event_id", "source_type")),
                     (_PlayerPoints, ("task_id", "team_id")),
                     (_Event, ("id",)), (_Task, ("id",))):
    for _n in _names:
        setattr(_cls, _n, _Col(_cls, _n))

_MODELS = {"EventProgress": _Progress, "EventTeam": _Team, "EventCompletion": _Ledger,
           "EventPlayerPoints": _PlayerPoints, "Event": _Event, "EventTask": _Task,
           "EventBingoCell": _Row, "EventBingoCompletion": _Row}


class _Q:
    def __init__(self, rows, cols=None):
        self._rows, self._cols = list(rows), cols

    def filter(self, *a):
        return self

    def with_for_update(self, *a, **k):
        return self

    def order_by(self, *a):
        return self

    def distinct(self):
        return self

    def first(self):
        rows = self.all()
        return rows[0] if rows else None

    def all(self):
        if self._cols:
            return [tuple(getattr(r, c) for c in self._cols) for r in self._rows]
        return list(self._rows)

    def count(self):
        return len(self._rows)

    def delete(self, synchronize_session=False):
        return 0


class _Sess:
    def __init__(self, store):
        self.store = store

    def query(self, *args):
        target = args[0]
        if isinstance(target, _Col):
            return _Q(self.store.get(target._model, []),
                      cols=[a._name for a in args])
        return _Q(self.store.get(target, []))

    def add(self, obj):
        self.store.setdefault(type(obj), []).append(obj)

    def flush(self):
        pass


@contextmanager
def _fake_models():
    dbm = sys.modules["db.models"]
    saved = {name: getattr(dbm, name) for name in _MODELS}
    for name, cls in _MODELS.items():
        setattr(dbm, name, cls)
    # apply_ledger_row reads the message config on every completion; the
    # conftest's ``services`` stub is not a package, so stand one module in.
    notif = ModuleType("services.event_notifications")
    notif.effective_message_config = lambda cfg: {}
    saved_notif = sys.modules.get("services.event_notifications")
    sys.modules["services.event_notifications"] = notif
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(dbm, name, value)
        if saved_notif is None:
            sys.modules.pop("services.event_notifications", None)
        else:
            sys.modules["services.event_notifications"] = saved_notif


@contextmanager
def _patched(**fns):
    saved = {name: getattr(engine, name) for name in fns}
    for name, fn in fns.items():
        setattr(engine, name, fn)
    try:
        yield
    finally:
        for name, fn in saved.items():
            setattr(engine, name, fn)


EVENT = {"id": 1, "kind": "standard", "has_bingo": False, "message_config": None}
ZULRAH_CFG = {"source_npcs": ["Zulrah"], "repeatable": True}


def _task(config, points=5, target_value=100_000, ttype="loot_value"):
    return {"id": 9, "type": ttype, "target_value": target_value, "points": points,
            "label": "Acquire 100k gp from Zulrah", "config": config}


class _Harness:
    """Drives apply_ledger_row against one (task, team) rollup."""

    def __init__(self, task, event=EVENT, progress=0, completed=False, score=0):
        self.task, self.event = task, event
        self.team = _Team(id=4, event_id=1, score=score)
        self.store = {_Team: [self.team]}
        if progress or completed:
            self.store[_Progress] = [_Progress(event_id=1, task_id=9, team_id=4,
                                               progress=progress, completed=completed,
                                               completed_at=None)]
        self.notifications, self.done_marks, self.milestones = [], [], []
        self.points_awarded = []
        self.cells_called = 0

    @property
    def progress(self):
        return self.store[_Progress][0]

    def apply(self, quantity):
        completion = SimpleNamespace(
            id=1, event_id=1, task_id=9, team_id=4, player_id=5, quantity=quantity,
            matched_target=None, source_type="drop", proof_url=None, note=None)

        def _cells(*a, **k):
            self.cells_called += 1
            return []

        with _fake_models(), _patched(
            _enqueue_notification=lambda s, nt, ev, pid, data: self.notifications.append((nt, data)),
            _publish=lambda *a, **k: None,
            _task_contributors=lambda *a, **k: [],
            _award_contribution_points=lambda s, ev, t, tid, c, pts: self.points_awarded.append(pts),
            _leader_snapshot=lambda *a, **k: None,
            _announce_lead_change=lambda *a, **k: None,
            mark_task_done=lambda r, e, tid, task_id: self.done_marks.append(task_id),
            _maybe_enqueue_progress=lambda s, e, t, tid, pid, pn, prev, cur, **k:
                self.milestones.append((prev, cur)),
            _complete_bingo_cells=_cells,
        ):
            return engine.apply_ledger_row(_Sess(self.store), None, self.event,
                                           self.task, completion)

    def completions(self):
        return [d for nt, d in self.notifications if nt == "event_completion"]


# ── Apply ────────────────────────────────────────────────────────────────────

class TestApply:
    def test_ordinary_task_completes_once(self):
        h = _Harness(_task({"source_npcs": ["Zulrah"]}))
        h.apply(250_000)
        assert h.team.score == 5 and h.progress.completed is True
        h.apply(150_000)  # the record gate drops these in production anyway
        assert h.team.score == 5
        assert len(h.completions()) == 1
        assert h.done_marks == [9]

    def test_first_lap_pays_and_stays_open(self):
        h = _Harness(_task(ZULRAH_CFG))
        res = h.apply(120_000)
        assert res["kind"] == "completion"
        assert res["completions"] == 1 and res["completed"] is False
        assert h.team.score == 5
        assert h.progress.completed is False  # record gate stays open
        assert h.done_marks == []             # effort keeps accruing
        (data,) = h.completions()
        assert data["task_label"] == "Acquire 100k gp from Zulrah"
        assert data["completions"] == 1 and data["max_completions"] is None
        assert h.cells_called == 1

    def test_later_laps_pay_again_with_count_label(self):
        h = _Harness(_task(ZULRAH_CFG))
        h.apply(120_000)
        h.apply(90_000)   # 210k: lap 2
        h.apply(50_000)   # 260k: no new lap
        assert h.team.score == 10
        labels = [d["task_label"] for d in h.completions()]
        assert labels == ["Acquire 100k gp from Zulrah",
                          "Acquire 100k gp from Zulrah (×2)"]
        # The bingo/board side effects belong to the FIRST completion only.
        assert h.cells_called == 1
        # Contribution shares cover every lap paid so far.
        assert h.points_awarded == [5, 10]

    def test_one_big_drop_finishes_several_laps(self):
        h = _Harness(_task(ZULRAH_CFG))
        res = h.apply(530_000)
        assert res["completions"] == 5
        assert h.team.score == 25
        (data,) = h.completions()
        assert data["points"] == 25
        assert data["task_label"].endswith("(×5)")

    def test_cap_closes_the_task(self):
        h = _Harness(_task({**ZULRAH_CFG, "max_completions": 2}))
        h.apply(100_000)
        assert h.progress.completed is False
        res = h.apply(400_000)
        assert res["completions"] == 2 and res["completed"] is True
        assert h.team.score == 10           # capped: 2 laps, not 5
        assert h.progress.completed is True
        assert h.done_marks == [9]
        h.apply(100_000)
        assert h.team.score == 10

    def test_milestones_measure_the_current_lap(self):
        h = _Harness(_task(ZULRAH_CFG))
        h.apply(120_000)
        h.apply(40_000)
        # 120k -> 160k on the running total is 20k -> 60k on lap two.
        assert h.milestones[-1] == (20_000, 60_000)

    def test_flag_is_inert_on_a_bingo_event(self):
        h = _Harness(_task(ZULRAH_CFG), event={**EVENT, "kind": "bingo"})
        h.apply(250_000)
        assert h.team.score == 5 and h.progress.completed is True
        h.apply(250_000)
        assert h.team.score == 5

    def test_count_tasks_repeat(self):
        h = _Harness(_task({"repeatable": True}, ttype="kc_target", target_value=50,
                           points=2))
        for _ in range(120):
            h.apply(1)
        assert h.team.score == 4
        assert [d["completions"] for d in h.completions()] == [1, 2]


# ── Revoke ───────────────────────────────────────────────────────────────────

def _revoke(task, event, *, stored, derived, completed=False, score=0):
    team = _Team(id=4, event_id=1, score=score)
    progress = _Progress(event_id=1, task_id=9, team_id=4, progress=stored,
                         completed=completed, completed_at=None)
    store = {_Team: [team], _Progress: [progress],
             _Event: [_Event(id=1)], _Task: [_Task(id=9)]}
    awarded = []
    completion = SimpleNamespace(id=1, event_id=1, task_id=9, team_id=4,
                                 player_id=5, source_type="drop", quantity=1)
    with _fake_models(), _patched(
        _event_to_dict=lambda row: event,
        _task_to_dict=lambda row: task,
        _derive_applied_progress=lambda s, t, tid: derived,
        _publish=lambda *a, **k: None,
        _lead_changes_announceable=lambda row: False,
        _announce_lead_change=lambda *a, **k: None,
        _task_contributors=lambda *a, **k: [],
        _award_contribution_points=lambda s, ev, t, tid, c, pts: awarded.append(pts),
    ):
        out = engine.revoke_ledger_row(_Sess(store), completion)
    return out, team, progress, awarded


class TestRevoke:
    def test_takes_back_the_lost_laps(self):
        out, team, progress, awarded = _revoke(
            _task(ZULRAH_CFG), EVENT, stored=320_000, derived=150_000, score=15)
        assert out["completions"] == 1
        assert team.score == 5
        assert progress.completed is False
        assert awarded == [5]

    def test_reopens_a_capped_task(self):
        out, team, progress, _ = _revoke(
            _task({**ZULRAH_CFG, "max_completions": 3}), EVENT,
            stored=300_000, derived=250_000, completed=True, score=15)
        assert out["completions"] == 2
        assert team.score == 10
        assert progress.completed is False

    def test_revoke_within_a_lap_changes_no_score(self):
        out, team, _, _ = _revoke(
            _task(ZULRAH_CFG), EVENT, stored=260_000, derived=210_000, score=10)
        assert out["completions"] == 2 and team.score == 10

    def test_ordinary_task_unchanged(self):
        out, team, progress, _ = _revoke(
            _task({"source_npcs": ["Zulrah"]}), EVENT,
            stored=300_000, derived=50_000, completed=True, score=5)
        assert team.score == 0 and progress.completed is False
        assert "completions" not in out


# ── Live-edit re-fold ────────────────────────────────────────────────────────

def _recompute(task, *, stored, derived, completed=False, score=0, **kw):
    team = _Team(id=4, event_id=1, score=score)
    progress = _Progress(event_id=1, task_id=9, team_id=4, progress=stored,
                         completed=completed, completed_at=None)
    store = {_Team: [team], _Progress: [progress], _Ledger: []}
    with _fake_models(), _patched(
        _event_to_dict=lambda row: EVENT,
        _task_to_dict=lambda row: task,
        _derive_applied_progress=lambda s, t, tid: derived,
        _publish=lambda *a, **k: None,
        _lead_changes_announceable=lambda row: False,
        _announce_lead_change=lambda *a, **k: None,
        _task_contributors=lambda *a, **k: [],
        _award_contribution_points=lambda *a, **k: None,
        reconcile_bingo_bonuses=lambda *a, **k: {},
    ):
        out = engine.recompute_task_rollups(_Sess(store), object(), object(), **kw)
    return out["teams"][4], team, progress


class TestRecompute:
    def test_lower_goal_pays_the_extra_laps(self):
        # 250k banked against 100k laps (2 paid); goal drops to 50k → 5 laps.
        summary, team, _ = _recompute(
            _task(ZULRAH_CFG, target_value=50_000), stored=250_000, derived=250_000,
            score=10, old_points=5,
            old_task={"type": "loot_value", "target_value": 100_000, "config": ZULRAH_CFG})
        assert summary["completions"] == 5 and summary["was_completions"] == 2
        assert team.score == 25

    def test_points_reprice_every_lap(self):
        summary, team, _ = _recompute(
            _task(ZULRAH_CFG, points=8), stored=300_000, derived=300_000,
            score=15, old_points=5)
        assert summary["score_delta"] == 9 and team.score == 24

    def test_turning_repeat_on_reopens_a_finished_task(self):
        summary, team, progress = _recompute(
            _task(ZULRAH_CFG), stored=100_000, derived=100_000, completed=True,
            score=5, old_points=5,
            old_task={"type": "loot_value", "target_value": 100_000,
                      "config": {"source_npcs": ["Zulrah"]}})
        assert progress.completed is False
        assert team.score == 5 and summary["completions"] == 1

    def test_turning_repeat_off_takes_back_extra_laps(self):
        summary, team, progress = _recompute(
            _task({"source_npcs": ["Zulrah"]}), stored=300_000, derived=300_000,
            score=15, old_points=5,
            old_task={"type": "loot_value", "target_value": 100_000, "config": ZULRAH_CFG})
        assert progress.completed is True
        assert team.score == 5 and summary["was_completions"] == 3

    def test_ordinary_uncomplete_takes_back_the_points_it_paid(self):
        # Points went 5 → 8 while the goal rose past the team's progress: the
        # team was paid 5, so 5 comes back off (not the new 8).
        summary, team, _ = _recompute(
            _task({"source_npcs": ["Zulrah"]}, points=8, target_value=500_000),
            stored=100_000, derived=100_000, completed=True, score=5, old_points=5)
        assert summary["completed"] is False and team.score == 0


class TestSyncRepeatFlags:
    def _sync(self, task, old_config, *, stored, completed):
        progress = _Progress(event_id=1, task_id=9, team_id=4, progress=stored,
                             completed=completed, completed_at=None)
        with _fake_models(), _patched(_event_to_dict=lambda row: EVENT,
                                      _task_to_dict=lambda row: task):
            changed = engine.sync_repeat_flags(
                _Sess({_Progress: [progress]}), object(), object(),
                {"type": task["type"], "target_value": 100_000, "config": old_config})
        return changed, progress

    def test_keep_and_turn_on_reopens(self):
        changed, progress = self._sync(_task(ZULRAH_CFG), None,
                                       stored=100_000, completed=True)
        assert changed == {4: False} and progress.completed is False

    def test_keep_and_turn_off_closes_a_lapped_task(self):
        changed, progress = self._sync(_task(None), ZULRAH_CFG,
                                       stored=300_000, completed=False)
        assert changed == {4: True} and progress.completed is True

    def test_no_repeat_change_is_a_noop(self):
        changed, _ = self._sync(_task(ZULRAH_CFG), ZULRAH_CFG,
                                stored=300_000, completed=False)
        assert changed == {}


class TestPendingProjection:
    def _project(self, task, applied, pending):
        rows = ([SimpleNamespace(status="auto", quantity=q, source_type="drop")
                 for q in applied]
                + [SimpleNamespace(status="pending", quantity=q, source_type="drop")
                   for q in pending])
        with _fake_models():
            return engine.pending_projection(_Sess({_Ledger: rows}), task, 4)

    def test_pending_finishing_the_next_lap(self):
        proj = self._project(_task(ZULRAH_CFG), [150_000], [60_000])
        assert proj["pending_complete"] is True

    def test_pending_inside_a_lap(self):
        proj = self._project(_task(ZULRAH_CFG), [150_000], [20_000])
        assert proj["pending_complete"] is False
