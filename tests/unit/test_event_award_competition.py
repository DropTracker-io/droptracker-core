"""Manual award on a sotw/botw race (POST /events/{id}/award): the
competition-specific validation and ledger-row shaping in
``event_admin._competition_award_fields``. The real ``services.competition``
is loaded by path and swapped in for the conftest stub."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

import web_api.routes.event_admin as ea

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "_competition_award_ut", os.path.join(_ROOT, "services", "competition.py"))
comp = importlib.util.module_from_spec(_spec)
sys.modules["_competition_award_ut"] = comp
_spec.loader.exec_module(comp)

BOTW = {"kind": "competition", "metric_kind": "boss",
        "npcs": ["Dagannoth Rex", "Dagannoth Prime"], "ranking": {"mode": "gained"},
        "bonus_rules": []}
SOTW = {"kind": "competition", "metric_kind": "skill", "skill": "mining",
        "ranking": {"mode": "gained"}, "bonus_rules": []}


class _Problem(Exception):
    def __init__(self, status, title, detail=""):
        super().__init__(title)
        self.status, self.title = status, title


@pytest.fixture(autouse=True)
def _real_competition(monkeypatch):
    monkeypatch.setitem(sys.modules, "services.competition", comp)

    def _abort(status, title, detail=""):
        raise _Problem(status, title, detail)
    monkeypatch.setattr(ea, "abort_problem", _abort)


def _fields(cfg=BOTW, **kw):
    args = dict(player_id=7, credit=None, complete=False, path_idx=None,
                matched_target=None, note=None)
    args.update(kw)
    return ea._competition_award_fields(
        SimpleNamespace(config=json.dumps(cfg)), **args)


def test_player_required():
    with pytest.raises(_Problem) as e:
        _fields(player_id=None)
    assert e.value.status == 422 and e.value.title == "Choose a player"


@pytest.mark.parametrize("kw", [{"complete": True}, {"path_idx": 0}])
def test_complete_and_path_refused(kw):
    with pytest.raises(_Problem):
        _fields(**kw)


def test_gained_default_untagged():
    assert _fields(note="WOM missed kills") == (None, "WOM missed kills")


def test_gained_note_that_looks_like_a_tag_is_defused():
    target, note = _fields(note="bonus:pet:1")
    assert comp.parse_bonus_note(note) is None


def test_gained_boss_must_be_raced():
    assert _fields(matched_target="dagannoth  REX")[0] == "dagannoth  REX"
    with pytest.raises(_Problem):
        _fields(matched_target="Zulrah")
    with pytest.raises(_Problem):
        _fields(cfg=SOTW, matched_target="Dagannoth Rex")


def test_bonus_is_tagged_manual():
    target, note = _fields(credit="bonus", note="missed pet")
    assert target is None
    assert comp.parse_bonus_note(note) == ("manual", comp.MANUAL_RULE_ID)
    assert note.endswith("| missed pet")
    assert _fields(credit="bonus")[1] == "bonus:manual:0"
    assert len(_fields(credit="bonus", note="x" * 255)[1]) == 255


def test_bonus_refuses_a_boss():
    with pytest.raises(_Problem):
        _fields(credit="bonus", matched_target="Dagannoth Rex")
