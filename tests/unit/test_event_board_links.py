"""web_api/routes/event_board.py (2026-09): the tile-payload rules for chutes &
ladders and required tiles, the settings patch for board style / exact
finish, and the tile row's link fields + legacy-kind alias.

The conftest stubs ``db``, so the route's imported vocabularies are
MagicMocks — monkeypatch the real tuples (the test_event_tasks_visibility
pattern) before exercising the validators."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import web_api.routes.event_board as eb
from web_api.common import ProblemException

KINDS = ("start", "normal", "required", "finish")
DIFFS = ("air", "water", "earth", "fire")


@pytest.fixture(autouse=True)
def real_vocab(monkeypatch):
    monkeypatch.setattr(eb, "EVENT_BOARD_TILE_KINDS", KINDS)
    monkeypatch.setattr(eb, "EVENT_TASK_DIFFICULTIES", DIFFS)


def _track(overrides=None, n=10):
    overrides = overrides or {}
    tiles = []
    for i in range(n):
        cell = {"idx": i, "x": 0.1 * i, "y": 0.5, "difficulty": "air",
                "tile_kind": "start" if i == 0 else "finish" if i == n - 1 else "normal"}
        cell.update(overrides.get(i, {}))
        tiles.append(cell)
    return tiles


def _rejects(tiles, fragment):
    with pytest.raises(ProblemException) as e:
        eb._validate_tiles_payload(tiles)
    assert e.value.status == 422
    assert fragment in (e.value.detail or ""), e.value.detail


class TestTileLinkRules:
    def test_plain_ladder_and_chute_pass(self):
        eb._validate_tiles_payload(_track({3: {"jump_to": 7}, 6: {"jump_to": 1}}))

    def test_self_link(self):
        _rejects(_track({3: {"jump_to": 3}}), "link to itself")

    def test_missing_target(self):
        _rejects(_track({3: {"jump_to": 42}}), "does not exist")

    def test_start_and_finish_cannot_link(self):
        _rejects(_track({0: {"jump_to": 4}}), "start")
        _rejects(_track({9: {"jump_to": 4}}), "finish")

    def test_links_never_chain(self):
        _rejects(_track({3: {"jump_to": 5}, 5: {"jump_to": 8}}), "can't chain")

    def test_ladder_may_not_cross_a_required_tile(self):
        _rejects(_track({3: {"jump_to": 7}, 5: {"tile_kind": "required"}}),
                 "past required tile 5")

    def test_chute_may_cross_a_required_tile(self):
        eb._validate_tiles_payload(_track({7: {"jump_to": 2}, 5: {"tile_kind": "required"}}))

    def test_ladder_may_end_on_a_required_tile(self):
        eb._validate_tiles_payload(_track({3: {"jump_to": 5}, 5: {"tile_kind": "required"}}))

    def test_complete_trigger_only_on_ladders_with_a_task(self):
        _rejects(_track({6: {"jump_to": 1, "jump_when": "complete"}}), "only a ladder")
        _rejects(_track({3: {"jump_to": 7, "jump_when": "complete", "difficulty": None}}),
                 "needs a task")
        eb._validate_tiles_payload(_track({3: {"jump_to": 7, "jump_when": "complete"}}))

    def test_bad_trigger_value(self):
        _rejects(_track({3: {"jump_to": 7, "jump_when": "later"}}), "jump_when")

    def test_bad_target_type(self):
        _rejects(_track({3: {"jump_to": "7"}}), "tile idx")
        _rejects(_track({3: {"jump_to": True}}), "tile idx")

    def test_legacy_special_kind_is_accepted_as_required(self):
        eb._validate_tiles_payload(_track({5: {"tile_kind": "special"}}))
        assert eb._normalize_tile_kind("special") == "required"
        assert eb._normalize_tile_kind(None) == "normal"


class TestTileRowLinks:
    def test_row_exposes_the_link(self):
        t = SimpleNamespace(idx=3, x=0.3, y=0.5, label=None, difficulty="air", task_id=None,
                            tile_kind="normal", config='{"jump_to": 7, "jump_when": "complete"}')
        row = eb._tile_row(t, {})
        assert (row["jump_to"], row["jump_when"]) == (7, "complete")
        concealed = eb._tile_row(t, {}, conceal=True)
        assert (concealed["jump_to"], concealed["jump_when"]) == (7, "complete")
        assert concealed["difficulty"] is None

    def test_row_without_a_link(self):
        t = SimpleNamespace(idx=3, x=0.3, y=0.5, label=None, difficulty=None, task_id=None,
                            tile_kind="special", config=None)
        row = eb._tile_row(t, {})
        assert (row["jump_to"], row["jump_when"]) == (None, None)
        assert row["tile_kind"] == "required"

    def test_corrupt_config_is_no_link(self):
        t = SimpleNamespace(idx=3, x=0.3, y=0.5, label=None, difficulty=None, task_id=None,
                            tile_kind="normal", config="{nope")
        assert eb._tile_row(t, {})["jump_to"] is None


class TestSettingsPatch:
    def test_style_and_exact_finish(self):
        out = eb._validate_settings_patch({"style": "chutes_ladders",
                                           "win": {"exact_finish": "bounce"}})
        assert out == {"style": "chutes_ladders", "win": {"exact_finish": "bounce"}}

    def test_unknown_style(self):
        with pytest.raises(ProblemException) as e:
            eb._validate_settings_patch({"style": "monopoly"})
        assert e.value.status == 422

    def test_unknown_exact_finish(self):
        with pytest.raises(ProblemException) as e:
            eb._validate_settings_patch({"win": {"exact_finish": "explode"}})
        assert e.value.status == 422
