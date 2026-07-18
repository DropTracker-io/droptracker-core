"""Bingo board save (PUT /events/{id}/bingo) — bind-vs-clone regression.

Event 9 grew byte-identical task pairs ("10 Armadyl Points" ×2, "10 Demonic
Points" ×2, …): every board save created a fresh ``bingo_auto`` clone for
library picks and inline new tasks even when the event's task list already
held an identical row, leaving the original orphaned forever — never
cell-bound, and the engine only matches cell-bound tasks on bingo events.
These pin the fix: an identical existing task is BOUND, not cloned; content
that genuinely differs still clones; explicit task_id references stay
reserved for their cells; and re-saving an already-polluted board binds the
original while the garbage collector drops the now-orphaned clone.

Same scripted-session harness as test_event_task_delete: each ``_S(...)``
batch answers the next query in order, so an extra or missing query fails
the test.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.event_admin as eva

from tests.unit.test_event_auth_modes import _S, _SessionCM

# Real tuples (the conftest stubs db, so the module-level imports in
# event_admin are MagicMocks and `x in EVENT_TASK_TYPES` would be False).
REAL_BOARD_SIZES = (3, 4, 5, 6, 7)
REAL_TASK_TYPES = (
    "item_collection", "kc_target", "xp_target", "ehp_target", "ehb_target",
    "pb_target", "skill_target", "loot_value", "custom",
)


class _FakeTask:
    """EventTask stand-in. Class-level attrs keep filter expressions happy;
    every construction is recorded so tests can assert nothing was cloned."""

    id = MagicMock()
    event_id = MagicMock()
    _seq = itertools.count(100)
    instances: list = []

    def __init__(self, **kw):
        self.id = next(_FakeTask._seq)
        self.visibility = "public"
        self.config = None
        self.requires_confirmation = False
        self.__dict__.update(kw)
        _FakeTask.instances.append(self)


class _FakeCell:
    event_id = MagicMock()
    id = MagicMock()

    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


@pytest.fixture(autouse=True)
def _fresh_fakes(monkeypatch):
    _FakeTask.instances = []
    monkeypatch.setattr(eva, "EventTask", _FakeTask)
    monkeypatch.setattr(eva, "EventBingoCell", _FakeCell)
    monkeypatch.setattr(eva, "EVENT_BOARD_SIZES", REAL_BOARD_SIZES)
    monkeypatch.setattr(eva, "EVENT_TASK_TYPES", REAL_TASK_TYPES)


def _wire(monkeypatch, session, body, save_calls):
    async def _fake_body():
        return body

    monkeypatch.setattr(eva, "current_user_id", lambda: 7)
    monkeypatch.setattr(eva, "json_body", _fake_body)
    monkeypatch.setattr(eva, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(eva, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(eva, "_assert_event_admin", lambda *a, **k: None)
    monkeypatch.setattr(eva, "_detail", lambda s, ev, viewer_id=None: {"bingo": {"saved": True}})
    monkeypatch.setattr(
        eva, "save_task_to_library",
        lambda s, ev, task, vis: (save_calls.append(task), vis)[1],
    )


def _event(**kw):
    base = dict(
        id=1, group_id=42, name="Ev", status="draft", starts_at=None,
        ends_at=None, activated_at=None, has_bingo=True, board_size=3,
        kind="bingo",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _task_row(id=41, **kw):
    """A hand-added task-list row (no bingo_auto marker unless given one)."""
    base = dict(
        event_id=1, type="custom", label="5 Brimstone Keys", target=None,
        target_value=None, points=5, requires_confirmation=False,
        visibility="public", config=None,
    )
    base.update(kw)
    return SimpleNamespace(id=id, **base)


def _board_body(*bound_cells, size=3):
    used = {c["idx"] for c in bound_cells}
    cells = list(bound_cells)
    cells.extend({"idx": i} for i in range(size * size) if i not in used)
    return {"size": size, "cells": cells}


def _cells_by_idx(session):
    return {c.idx: c for c in session.added if isinstance(c, _FakeCell)}


# An inline "custom" task (custom validation touches no tables, keeping the
# query script to the board save's own reads) byte-identical to _task_row().
NT = {"type": "custom", "label": "5 Brimstone Keys", "points": 5}


class TestBoardSaveBindsExistingTasks:
    async def test_new_task_binds_identical_existing_row(self, client, monkeypatch):
        orig = _task_row()
        # Queries: event, event task list, old cells (none).
        s = _S([_event()], [orig], [])
        saves = []
        _wire(monkeypatch, s, _board_body({"idx": 0, "new_task": dict(NT)}), saves)
        r = await client.put("/api/v1/events/1/bingo")
        assert r.status_code == 200
        assert s.committed
        assert _FakeTask.instances == []      # bound, not cloned
        assert saves == []                    # no library re-save on reuse
        cells = _cells_by_idx(s)
        assert len(cells) == 9
        assert cells[0].task_id == orig.id
        assert all(cells[i].task_id is None for i in range(1, 9))
        assert orig.config is None            # original not stamped bingo_auto
        assert s._batches == []

    async def test_differing_content_still_clones(self, client, monkeypatch):
        orig = _task_row(points=10)           # points differ from the payload
        s = _S([_event()], [orig], [])
        saves = []
        _wire(monkeypatch, s, _board_body({"idx": 0, "new_task": dict(NT)}), saves)
        r = await client.put("/api/v1/events/1/bingo")
        assert r.status_code == 200
        assert len(_FakeTask.instances) == 1
        clone = _FakeTask.instances[0]
        assert json.loads(clone.config)["bingo_auto"] is True
        assert saves == [clone]
        assert _cells_by_idx(s)[0].task_id == clone.id

    async def test_each_row_binds_at_most_one_cell(self, client, monkeypatch):
        orig = _task_row()
        s = _S([_event()], [orig], [])
        saves = []
        _wire(monkeypatch, s, _board_body(
            {"idx": 0, "new_task": dict(NT)},
            {"idx": 1, "new_task": dict(NT)},
        ), saves)
        r = await client.put("/api/v1/events/1/bingo")
        assert r.status_code == 200
        # First identical cell claims the original; the second gets a clone.
        assert len(_FakeTask.instances) == 1
        cells = _cells_by_idx(s)
        assert cells[0].task_id == orig.id
        assert cells[1].task_id == _FakeTask.instances[0].id

    async def test_explicit_task_id_reference_is_reserved(self, client, monkeypatch):
        orig = _task_row()
        s = _S([_event()], [orig], [])
        saves = []
        _wire(monkeypatch, s, _board_body(
            {"idx": 1, "new_task": dict(NT)},
            {"idx": 4, "task_id": orig.id},
        ), saves)
        r = await client.put("/api/v1/events/1/bingo")
        assert r.status_code == 200
        # The new_task cell (processed first, lower idx) must not steal the
        # row another cell binds by id.
        assert len(_FakeTask.instances) == 1
        cells = _cells_by_idx(s)
        assert cells[4].task_id == orig.id
        assert cells[1].task_id == _FakeTask.instances[0].id

    async def test_resave_binds_original_and_gcs_the_clone(self, client, monkeypatch):
        # The event-9 shape: hand-added original (53) orphaned, byte-identical
        # bingo_auto clone (58) bound to the old board. Re-saving the same
        # content binds the ORIGINAL and garbage-collects the dropped clone.
        orig = _task_row(id=53, label="10 Armadyl Points")
        clone = _task_row(id=58, label="10 Armadyl Points",
                          config='{"bingo_auto": true}')
        old_cell = SimpleNamespace(id=201, task_id=clone.id)
        # Queries: event, task list, old cells, old tasks (marker check),
        # bingo-completion bulk delete, cell bulk delete, then the GC of task
        # 58: ledger probe (empty), progress bulk delete, task bulk delete.
        s = _S(
            [_event()], [orig, clone], [old_cell], [clone],
            [], [old_cell], [], [], [clone],
        )
        saves = []
        _wire(monkeypatch, s, _board_body(
            {"idx": 0, "new_task": {"type": "custom", "label": "10 Armadyl Points", "points": 5}},
        ), saves)
        r = await client.put("/api/v1/events/1/bingo")
        assert r.status_code == 200
        assert _FakeTask.instances == []
        assert _cells_by_idx(s)[0].task_id == orig.id
        # Every scripted batch consumed ⇒ the clone's GC deletes actually ran.
        assert s._batches == []

    async def test_library_pick_binds_identical_existing_row(self, client, monkeypatch):
        config_a = '{"kind": "point_collection", "items": [{"item_name": "Armadyl helmet", "points": 2.0}]}'
        config_b = '{"items": [{"points": 2.0, "item_name": "Armadyl helmet"}], "kind": "point_collection"}'
        orig = _task_row(
            id=53, type="item_collection", label="10 Armadyl Points",
            target=None, target_value=10, points=5, config=config_b,
        )
        preset = SimpleNamespace(
            id=77, type="item_collection", name="10 Armadyl Points",
            target=None, target_value=10, default_points=5, config=config_a,
            active=True,
        )
        # Queries: event, library presets, event task list, old cells.
        s = _S([_event()], [preset], [orig], [])
        saves = []
        _wire(monkeypatch, s, _board_body({"idx": 0, "library_item_id": 77}), saves)
        r = await client.put("/api/v1/events/1/bingo")
        assert r.status_code == 200
        assert _FakeTask.instances == []      # config key order didn't fool it
        assert _cells_by_idx(s)[0].task_id == orig.id
        assert orig.config == config_b        # untouched
        assert s._batches == []


class TestTaskIdentity:
    def test_marker_and_key_order_insensitive(self):
        a = eva._task_identity(
            "item_collection", "10 Armadyl Points", None, 10, 5, False,
            '{"kind": "any_of", "items": ["A"], "bingo_auto": true}')
        b = eva._task_identity(
            "item_collection", "10 Armadyl Points", None, 10, 5, False,
            '{"items": ["A"], "kind": "any_of"}')
        assert a == b

    def test_marker_only_config_equals_no_config(self):
        a = eva._task_identity("item_collection", "1 Dragon Limbs",
                               "Dragon limbs", 1, 0, False, '{"bingo_auto": true}')
        b = eva._task_identity("item_collection", "1 Dragon Limbs",
                               "Dragon limbs", 1, 0, False, None)
        assert a == b

    def test_label_and_target_match_case_insensitively(self):
        a = eva._task_identity("kc_target", "50× Zulrah", "Zulrah", 50, 5, False, None)
        b = eva._task_identity("kc_target", "  50× ZULRAH ", "zulrah", 50, 5, False, None)
        assert a == b

    def test_differing_points_or_confirmation_are_different_tasks(self):
        base = ("custom", "Chore", None, None)
        assert (eva._task_identity(*base, 5, False, None)
                != eva._task_identity(*base, 10, False, None))
        assert (eva._task_identity(*base, 5, True, None)
                != eva._task_identity(*base, 5, False, None))

    def test_dict_config_accepted(self):
        a = eva._task_identity("item_collection", "T", None, 1, 0, False,
                               {"kind": "any_of", "items": ["A"], "bingo_auto": True})
        b = eva._task_identity("item_collection", "T", None, 1, 0, False,
                               '{"kind": "any_of", "items": ["A"]}')
        assert a == b
