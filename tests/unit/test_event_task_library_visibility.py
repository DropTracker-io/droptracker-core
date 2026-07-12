"""Unit tests for the task-library sharing helpers (public/private task saves).

Covers ``clean_task_visibility`` (request-body validation) and
``save_task_to_library`` (the per-group upsert of a task's reusable library
copy). The conftest stubs ``db``, so the ORM classes are replaced with plain
fakes and queries run against a scripted session, mirroring
test_event_auth_modes.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.events as evr
from web_api.common import ProblemException


class _Q:
    def __init__(self, result):
        self._r = result

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._r[0] if self._r else None


class _S:
    """Scripted session: each query() pops the next scripted result."""

    def __init__(self, results):
        self._results = list(results)
        self.added = []

    def query(self, *a, **k):
        return _Q(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)


class FakeLibraryItem:
    # Class-level attrs so filter expressions (source == …, name == …) resolve.
    source = MagicMock()
    name = MagicMock()
    group_id = MagicMock()

    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture(autouse=True)
def _unstub(monkeypatch):
    """The conftest stubs db as a MagicMock — restore real semantics for the
    pieces these helpers touch."""
    monkeypatch.setattr(evr, "EVENT_TASK_VISIBILITIES", ("public", "private"))
    monkeypatch.setattr(evr, "EventTaskLibraryItem", FakeLibraryItem)
    monkeypatch.setattr(evr, "func", MagicMock())


def _task(**overrides):
    base = dict(
        label="Collect a Twisted bow",
        type="item_collection",
        target="Twisted bow",
        target_value=1,
        points=25,
        config=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _event(group_id=7):
    return SimpleNamespace(id=1, group_id=group_id)


# ── clean_task_visibility ────────────────────────────────────────────────────

def test_visibility_defaults_public_on_create():
    assert evr.clean_task_visibility({}) == "public"


def test_visibility_absent_on_patch_means_leave_alone():
    assert evr.clean_task_visibility({}, default=None) is None


def test_visibility_accepts_both_values():
    assert evr.clean_task_visibility({"visibility": "public"}) == "public"
    assert evr.clean_task_visibility({"visibility": "private"}, default=None) == "private"


@pytest.mark.parametrize("bad", ["friends", "", None, 1, True])
def test_visibility_rejects_garbage(bad):
    with pytest.raises(ProblemException) as exc:
        evr.clean_task_visibility({"visibility": bad})
    assert exc.value.status == 422


# ── save_task_to_library ─────────────────────────────────────────────────────

def test_save_creates_group_row():
    s = _S([[]])  # no existing row
    evr.save_task_to_library(s, _event(group_id=7), _task(), "private")
    assert len(s.added) == 1
    row = s.added[0]
    assert row.source == "group"
    assert row.group_id == 7
    assert row.name == "Collect a Twisted bow"
    assert row.type == "item_collection"
    assert row.target == "Twisted bow"
    assert row.target_value == 1
    assert row.default_points == 25
    assert row.visibility == "private"
    assert row.active is True


def test_save_updates_existing_row_instead_of_duplicating():
    existing = FakeLibraryItem(
        name="Collect a Twisted bow", source="group", group_id=7,
        visibility="private", default_points=1,
    )
    s = _S([[existing]])
    evr.save_task_to_library(s, _event(group_id=7), _task(points=50), "public")
    assert s.added == []  # upsert, not insert
    assert existing.visibility == "public"
    assert existing.default_points == 50


def test_save_strips_bingo_auto_marker():
    s = _S([[]])
    task = _task(config='{"kind": "any_of", "items": ["A", "B"], "bingo_auto": true}')
    evr.save_task_to_library(s, _event(), task, "public")
    assert '"bingo_auto"' not in (s.added[0].config or "")
    assert '"any_of"' in s.added[0].config


def test_save_marker_only_config_becomes_null():
    s = _S([[]])
    evr.save_task_to_library(s, _event(), _task(config='{"bingo_auto": true}'), "public")
    assert s.added[0].config is None


def test_save_truncates_name_and_target_to_column_width():
    s = _S([[]])
    evr.save_task_to_library(
        s, _event(), _task(label="x" * 300, target="y" * 300), "public")
    assert len(s.added[0].name) == 120
    assert len(s.added[0].target) == 120


def test_save_skips_blank_labels():
    s = _S([])  # any query would pop from an empty script and fail
    evr.save_task_to_library(s, _event(), _task(label="   "), "public")
    assert s.added == []
