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

    def all(self):
        return list(self._r)


class _S:
    """Scripted session: each query() pops the next scripted result.

    ``save_task_to_library`` issues two queries per call: the group's own
    name-keyed row (``.first()``), then the requirement-duplicate candidates
    (``.all()``)."""

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
    active = MagicMock()
    type = MagicMock()
    target = MagicMock()
    target_value = MagicMock()

    def __init__(self, **kw):
        self.id = None
        self.config = None
        self.visibility = "public"
        self.__dict__.update(kw)


@pytest.fixture(autouse=True)
def _unstub(monkeypatch):
    """The conftest stubs db as a MagicMock — restore real semantics for the
    pieces these helpers touch."""
    monkeypatch.setattr(evr, "EVENT_TASK_VISIBILITIES", ("public", "private"))
    monkeypatch.setattr(evr, "EventTaskLibraryItem", FakeLibraryItem)
    monkeypatch.setattr(evr, "func", MagicMock())
    monkeypatch.setattr(evr, "sa_or", MagicMock())


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

def test_visibility_defaults_private_on_create():
    # Audit: public-by-default quietly shipped clan-specific labels into the
    # shared cross-group library — sharing is now the deliberate choice.
    assert evr.clean_task_visibility({}) == "private"


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
    s = _S([[], []])  # no existing row, no requirement duplicates
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
    s = _S([[existing], []])
    evr.save_task_to_library(s, _event(group_id=7), _task(points=50), "public")
    assert s.added == []  # upsert, not insert
    assert existing.visibility == "public"
    assert existing.default_points == 50


def test_save_strips_bingo_auto_marker():
    s = _S([[], []])
    task = _task(config='{"kind": "any_of", "items": ["A", "B"], "bingo_auto": true}')
    evr.save_task_to_library(s, _event(), task, "public")
    assert '"bingo_auto"' not in (s.added[0].config or "")
    assert '"any_of"' in s.added[0].config


def test_save_marker_only_config_becomes_null():
    s = _S([[], []])
    evr.save_task_to_library(s, _event(), _task(config='{"bingo_auto": true}'), "public")
    assert s.added[0].config is None


def test_save_truncates_name_and_target_to_column_width():
    s = _S([[], []])
    evr.save_task_to_library(
        s, _event(), _task(label="x" * 300, target="y" * 300), "public")
    assert len(s.added[0].name) == 120
    assert len(s.added[0].target) == 120


def test_save_skips_blank_labels():
    s = _S([])  # any query would pop from an empty script and fail
    evr.save_task_to_library(s, _event(), _task(label="   "), "public")
    assert s.added == []


# ── save_task_to_library requirement dedupe ─────────────────────────────────

def _curated(**kw):
    base = dict(
        id=99, name="Collect a Twisted bow", source="legacy_v1", group_id=None,
        visibility="public", type="item_collection", target="Twisted bow",
        target_value=1, config=None,
    )
    base.update(kw)
    return FakeLibraryItem(**base)


def test_copying_an_existing_preset_saves_nothing():
    # Same name AND same requirements as a public preset ⇒ the picker row
    # already exists; a group copy would only duplicate it.
    s = _S([[], [_curated()]])
    out = evr.save_task_to_library(s, _event(group_id=7), _task(), "public")
    assert s.added == []
    assert out == "public"


def test_same_requirements_new_name_public_save_demoted_to_private():
    s = _S([[], [_curated()]])
    out = evr.save_task_to_library(
        s, _event(group_id=7), _task(label="Get a bow of the twisted kind"), "public")
    assert out == "private"
    assert len(s.added) == 1
    assert s.added[0].visibility == "private"
    assert s.added[0].name == "Get a bow of the twisted kind"


def test_private_save_with_public_duplicate_stays_private():
    s = _S([[], [_curated()]])
    out = evr.save_task_to_library(
        s, _event(group_id=7), _task(label="Our tbow task"), "private")
    assert out == "private"
    assert s.added[0].visibility == "private"


def test_other_groups_private_duplicate_does_not_demote():
    hidden = _curated(id=50, name="Their tbow task", source="group",
                      group_id=42, visibility="private")
    s = _S([[], [hidden]])
    out = evr.save_task_to_library(
        s, _event(group_id=7), _task(label="Our tbow task"), "public")
    assert out == "public"
    assert s.added[0].visibility == "public"


def test_config_equality_ignores_key_order_and_whitespace():
    preset = _curated(
        name="Barrows points", target=None, target_value=10,
        config='{"kind": "point_collection", "items": [{"item_name": "Ahrim\'s hood", "points": 2.0}]}',
    )
    task = _task(
        label="Barrows points", target=None, target_value=10,
        config='{"items":[{"points":2.0,"item_name":"Ahrim\'s hood"}],"kind":"point_collection"}',
    )
    s = _S([[], [preset]])
    out = evr.save_task_to_library(s, _event(group_id=7), task, "public")
    assert s.added == []  # recognized as the same preset
    assert out == "public"


def test_custom_tasks_dedupe_on_name_only():
    # Two free-form manual tasks with empty goal fields are different tasks.
    other = _curated(name="Get a haircut", type="custom", target=None,
                     target_value=None, config=None)
    s = _S([[], [other]])
    out = evr.save_task_to_library(
        s, _event(group_id=7),
        _task(label="Hug Bob the cat", type="custom", target=None, target_value=None),
        "public")
    assert out == "public"
    assert s.added[0].visibility == "public"


def test_custom_task_with_same_name_is_a_copy():
    other = _curated(name="Get a haircut", type="custom", target=None,
                     target_value=None, config=None)
    s = _S([[], [other]])
    out = evr.save_task_to_library(
        s, _event(group_id=7),
        _task(label="Get a haircut", type="custom", target=None, target_value=None),
        "public")
    assert s.added == []
    assert out == "public"


def test_private_save_never_touches_own_public_preset():
    # Library-copy independence: a template copied from the picker lands as a
    # private task; editing that task (a private re-save under the preset's
    # name) must leave the group's shared PUBLIC preset untouched — not
    # rewrite its requirements, and not unshare it.
    own_public = FakeLibraryItem(
        id=7, name="Collect a Twisted bow", source="group", group_id=7,
        visibility="public", type="item_collection", target="Twisted bow",
        target_value=1, config=None, default_points=25,
    )
    s = _S([[own_public]])  # only the name-keyed lookup runs
    out = evr.save_task_to_library(
        s, _event(group_id=7), _task(target="Scythe of vitur", points=99), "private")
    assert out == "private"
    assert s.added == []
    assert own_public.visibility == "public"
    assert own_public.target == "Twisted bow"
    assert own_public.default_points == 25


def test_private_save_still_updates_own_private_preset():
    own_private = FakeLibraryItem(
        id=8, name="Collect a Twisted bow", source="group", group_id=7,
        visibility="private", type="item_collection", target="Twisted bow",
        target_value=1, config=None, default_points=1,
    )
    s = _S([[own_private], [own_private]])
    out = evr.save_task_to_library(s, _event(group_id=7), _task(points=50), "private")
    assert out == "private"
    assert s.added == []
    assert own_private.default_points == 50
    assert own_private.visibility == "private"


def test_updating_own_row_does_not_dedupe_against_itself():
    own = FakeLibraryItem(
        id=7, name="Collect a Twisted bow", source="group", group_id=7,
        visibility="public", type="item_collection", target="Twisted bow",
        target_value=1, config=None,
    )
    # Row lookup finds the group's own row; the candidate scan returns it too.
    s = _S([[own], [own]])
    out = evr.save_task_to_library(s, _event(group_id=7), _task(points=50), "public")
    assert out == "public"
    assert own.visibility == "public"
    assert own.default_points == 50
    assert s.added == []
