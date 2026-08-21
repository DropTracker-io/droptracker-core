"""Unit tests for the non-drop half of scripts/prune_drop_images.

The drop path is DB-driven and covered by ``test_prune_drop_images*``. This
covers the filesystem sweep added 2026-08-19, whose risk profile is different:
it deletes by *mtime* off a directory walk, with no DB row to confirm against.
The things that must hold are that it only ever touches non-drop type dirs,
that it honours recap protection, and that ``--apply`` gates every unlink.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def prune():
    spec = importlib.util.spec_from_file_location(
        "_prune_non_drop_under_test", REPO_ROOT / "scripts" / "prune_drop_images.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _mkfile(root: Path, rel: str, age_days: float, size: int = 16) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def tree(prune, tmp_path, monkeypatch):
    """A miniature user-upload tree rooted at tmp_path."""
    monkeypatch.setattr(prune, "LOCAL_ROOT", str(tmp_path) + os.sep)
    return tmp_path


def _sweep(prune, tree, apply, retention_days=30, protected=frozenset()):
    cutoff = datetime.now() - timedelta(days=retention_days)
    snap = io.StringIO()
    totals = prune.prune_non_drop_images(
        cutoff, protected, snap, apply, retention_days)
    return totals, snap.getvalue()


# ── the type boundary ────────────────────────────────────────────────────────

def test_drop_dir_is_never_swept(prune, tree):
    """`drop` has a value to weigh, so it belongs to the DB-driven scan only.

    A filesystem sweep would delete an old-but-valuable drop screenshot the
    value test is meant to keep forever — the one thing this must not do.
    """
    old_drop = _mkfile(tree, "123/drop/Zulrah/Coal_0.jpg", age_days=400)
    _sweep(prune, tree, apply=True)
    assert old_drop.exists()


def test_each_non_drop_type_is_swept(prune, tree):
    victims = {}
    for submission_type in prune.NON_DROP_TYPES:
        victims[submission_type] = _mkfile(
            tree, f"123/{submission_type}/old.jpg", age_days=400)
    _sweep(prune, tree, apply=True)
    for submission_type, path in victims.items():
        assert not path.exists(), f"{submission_type} was not swept"


# ── the retention window ─────────────────────────────────────────────────────

def test_recent_files_survive(prune, tree):
    fresh = _mkfile(tree, "123/level_up/fresh.jpg", age_days=2)
    stale = _mkfile(tree, "123/level_up/stale.jpg", age_days=99)
    _sweep(prune, tree, apply=True)
    assert fresh.exists()
    assert not stale.exists()


def test_file_exactly_inside_window_survives(prune, tree):
    edge = _mkfile(tree, "123/level_up/edge.jpg", age_days=29.9)
    _sweep(prune, tree, apply=True)
    assert edge.exists()


def test_retention_window_is_configurable(prune, tree):
    path = _mkfile(tree, "123/clog/x.jpg", age_days=10)
    _sweep(prune, tree, apply=True, retention_days=60)
    assert path.exists()
    _sweep(prune, tree, apply=True, retention_days=7)
    assert not path.exists()


# ── safety gates ─────────────────────────────────────────────────────────────

def test_dry_run_deletes_nothing_but_still_reports(prune, tree):
    path = _mkfile(tree, "123/level_up/old.jpg", age_days=400, size=2048)
    totals, log = _sweep(prune, tree, apply=False)
    assert path.exists(), "dry run must not unlink"
    assert totals["level_up"] == (1, 2048)
    assert str(path) in log


def test_recap_protected_paths_survive(prune, tree):
    keep = _mkfile(tree, "123/clog/keep.jpg", age_days=400)
    drop_it = _mkfile(tree, "123/clog/go.jpg", age_days=400)
    _sweep(prune, tree, apply=True, protected={str(keep)})
    assert keep.exists()
    assert not drop_it.exists()


def test_every_deletion_is_logged_before_it_happens(prune, tree):
    path = _mkfile(tree, "123/pb/old.jpg", age_days=400, size=99)
    _, log = _sweep(prune, tree, apply=True)
    row = [ln for ln in log.splitlines() if str(path) in ln]
    assert len(row) == 1
    _id, logged_path, size, action = row[0].split("\t")
    assert logged_path == str(path)
    assert size == "99"
    assert action == "deleted_pb"


def test_unrelated_top_level_dirs_are_ignored(prune, tree):
    """The walk keys off `*/{type}/`, so a sibling tree must be untouched."""
    bystander = _mkfile(tree, "123/notatype/old.jpg", age_days=400)
    _sweep(prune, tree, apply=True)
    assert bystander.exists()


def test_nested_subdirectories_are_reached(prune, tree):
    """Real paths carry an extra level (`level_up/unknown/foo.jpg`)."""
    nested = _mkfile(tree, "123/level_up/unknown/deep.jpg", age_days=400)
    _sweep(prune, tree, apply=True)
    assert not nested.exists()


def test_sweep_is_idempotent(prune, tree):
    _mkfile(tree, "123/quest/old.jpg", age_days=400)
    first, _ = _sweep(prune, tree, apply=True)
    second, _ = _sweep(prune, tree, apply=True)
    assert first["quest"][0] == 1
    assert second["quest"] == (0, 0)


def test_missing_root_is_not_an_error(prune, tmp_path, monkeypatch):
    monkeypatch.setattr(prune, "LOCAL_ROOT", str(tmp_path / "gone") + os.sep)
    totals, log = _sweep(prune, tmp_path, apply=True)
    assert all(v == (0, 0) for v in totals.values())
    assert log == ""


# ── the type/table mapping ───────────────────────────────────────────────────

def test_drop_absent_from_non_drop_map(prune):
    assert "drop" not in prune.NON_DROP_TYPES


def test_unreferenced_types_map_to_none(prune):
    """These are why the sweep exists: nothing in the DB points at them."""
    for submission_type in ("level_up", "pet", "experience_milestone"):
        assert prune.NON_DROP_TYPES[submission_type] is None


def test_referenced_types_name_a_table_and_pk(prune):
    for submission_type in ("clog", "ca", "pb", "quest", "death"):
        table, pk = prune.NON_DROP_TYPES[submission_type]
        assert isinstance(table, str) and table
        assert isinstance(pk, str) and pk
