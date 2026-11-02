"""SOTW/BOTW metric edits on a race backed by a WiseOldMan competition
(``_competition_metric_guard`` in web_api/routes/events.py): linked races
lock their metric, created races send changed WOM metrics back out, and a boss
race must keep at least one boss WiseOldMan tracks. Also covers the
partial-boss prompt (``_abort_partial_wom``).

Real CompetitionConfig / wom_boss_plan / slug mapping; only the competition
row and task lookups are faked."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

import web_api.routes.events as evr
from web_api.common import ProblemException

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


competition = _load("_competition_for_guard", "services/competition.py")
cw = _load("_competition_wom_for_guard", "services/competition_wom.py")
womutils = _load("_wiseoldman_for_guard", "utils/wiseoldman.py")


def _boss_cfg(*npcs):
    return {"kind": "competition", "metric_kind": "boss", "npcs": list(npcs)}


@pytest.fixture()
def wire(monkeypatch):
    """Point the guard's lazy imports at real code plus a fake row/task."""
    state = SimpleNamespace(row=None, task=None)
    setup = SimpleNamespace(competition_row=lambda s, eid: state.row,
                            competition_task=lambda s, eid: state.task)
    monkeypatch.setitem(sys.modules, "services.competition", competition)
    monkeypatch.setitem(sys.modules, "services.competition_setup", setup)
    monkeypatch.setitem(sys.modules, "services.competition_wom", cw)
    stub_wom = sys.modules.get("utils.wiseoldman")
    if stub_wom is not None:
        monkeypatch.setattr(stub_wom, "wom_boss_metric",
                            womutils.wom_boss_metric, raising=False)
        monkeypatch.setattr(stub_wom, "wom_skill_metric",
                            womutils.wom_skill_metric, raising=False)

    def _set(mode, cfg):
        state.row = (SimpleNamespace(wom_competition_id=99, source_mode=mode)
                     if mode else None)
        state.task = SimpleNamespace(config=json.dumps(cfg))
    return _set


EV = SimpleNamespace(id=1)


def _guard(cfg):
    return evr._competition_metric_guard(None, EV, cfg)


class TestMetricGuard:
    def test_no_wom_link_is_free(self, wire):
        wire(None, _boss_cfg("Zulrah"))
        assert _guard(_boss_cfg("Vorkath")) is None

    def test_unchanged_metric_sends_nothing(self, wire):
        wire("created", _boss_cfg("Zulrah", "Vorkath"))
        assert _guard(_boss_cfg("vorkath", "Zulrah")) is None

    def test_linked_race_locks_bosses(self, wire):
        wire("linked", _boss_cfg("Zulrah"))
        with pytest.raises(ProblemException) as exc:
            _guard(_boss_cfg("Zulrah", "Vorkath"))
        assert exc.value.status == 409
        assert exc.value.extra["code"] == "wom_linked_metric_locked"

    def test_created_race_mirrors_new_boss_set(self, wire):
        wire("created", _boss_cfg("Zulrah"))
        assert _guard(_boss_cfg("Zulrah", "Vorkath")) == ["zulrah", "vorkath"]

    def test_unsupported_boss_added_needs_no_wom_change(self, wire):
        # WOM's set is unchanged; the new boss races plugin-only.
        wire("created", _boss_cfg("Zulrah"))
        assert _guard(_boss_cfg("Zulrah", "Definitely Not A Boss")) is None

    def test_created_race_needs_one_wom_boss(self, wire):
        wire("created", _boss_cfg("Zulrah"))
        with pytest.raises(ProblemException) as exc:
            _guard(_boss_cfg("Definitely Not A Boss"))
        assert exc.value.extra["code"] == "wom_no_supported_bosses"


class TestPartialPrompt:
    def test_names_both_choices(self):
        plan = {"slugs": ["zulrah"], "supported": ["Zulrah"],
                "unsupported": ["Definitely Not A Boss"]}
        with pytest.raises(ProblemException) as exc:
            evr._abort_partial_wom(plan, linking=False)
        assert exc.value.status == 409
        assert exc.value.extra == {"code": "wom_partial_bosses",
                                   "supported": ["Zulrah"],
                                   "unsupported": ["Definitely Not A Boss"]}
        assert "DropTracker alone" in exc.value.detail
        assert "create the competition for the 1 boss" in exc.value.detail
