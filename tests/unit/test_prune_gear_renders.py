"""Retention for rendered character images (scripts/prune_gear_renders).

What these pin is the safety envelope: only ``{fp}.png`` / ``{fp}-avatar.png``
under the models prefix are ever candidates (a ``.glb`` model is never one),
a protected fingerprint keeps its picture at any age, young renders stay,
dry-run deletes nothing, and a batch failure is not counted as freed. The
local sweep is the same policy over a temp tree.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

OLD = datetime.now(timezone.utc) - timedelta(days=20)
NEW = datetime.now(timezone.utc) - timedelta(days=1)


class FakeB2:
    MODELS_PREFIX = "dt_img/models"

    def __init__(self, objects, fail=()):
        self.objects = dict(objects)  # key -> (size, last_modified)
        self.batches: list[list[str]] = []
        self._fail = set(fail)

    def list_keys(self, prefix):
        for key, (size, lm) in sorted(self.objects.items()):
            if key.startswith(prefix):
                yield {"key": key, "size": size, "etag": "e", "last_modified": lm}

    def delete_keys(self, keys):
        self.batches.append(list(keys))
        for k in keys:
            if k not in self._fail:
                self.objects.pop(k, None)
        return {k for k in keys if k in self._fail}

    @property
    def deleted(self):
        return sorted(k for b in self.batches for k in b if k not in self._fail)


@pytest.fixture
def prune():
    spec = importlib.util.spec_from_file_location(
        "_prune_renders_under_test", REPO_ROOT / "scripts" / "prune_gear_renders.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _sweep(prune, fake, protected=None, apply=True, limit=0, days=5):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    snap = _Snap()
    stats = prune.sweep_b2(fake, cutoff, protected or {}, snap, apply, limit)
    return stats, snap.lines


class _Snap:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)


class TestNaming:
    @pytest.mark.parametrize("name, expected", [
        ("abcdef12.png", ("abcdef12", False)),
        ("abcdef12-avatar.png", ("abcdef12", True)),
        ("abcdef12.glb", None),
        ("abcdef12-pet.glb", None),
        ("ABCDEF12.png", None),          # fingerprints are lowercase hex
        ("../etc/passwd.png", None),
        ("", None),
    ])
    def test_only_renders_and_crops_parse(self, prune, name, expected):
        assert prune.parse_render_name(name) == expected


class TestB2Sweep:
    def test_aged_unprotected_renders_and_their_crops_go(self, prune):
        fake = FakeB2({
            "dt_img/models/7/aaaa.png": (100, OLD),
            "dt_img/models/7/aaaa-avatar.png": (10, OLD),
            "dt_img/models/7/aaaa.glb": (50, OLD),          # never a candidate
            "dt_img/models/7/aaaa-pet.glb": (50, OLD),
            "dt_img/models/7/bbbb.png": (100, NEW),          # inside the window
        })

        stats, lines = _sweep(prune, fake)

        assert fake.deleted == [
            "dt_img/models/7/aaaa-avatar.png",
            "dt_img/models/7/aaaa.png",
        ]
        assert stats["removed"] == 2 and stats["freed"] == 110
        assert "dt_img/models/7/aaaa.glb" in fake.objects
        assert "dt_img/models/7/bbbb.png" in fake.objects
        assert len(lines) == 2 and lines[0].startswith("7\t")

    def test_protected_fingerprint_keeps_its_picture_at_any_age(self, prune):
        fake = FakeB2({
            "dt_img/models/7/aaaa.png": (100, OLD),
            "dt_img/models/7/aaaa-avatar.png": (10, OLD),
            "dt_img/models/7/cccc.png": (100, OLD),
            # Same fingerprint, another player: protection is per player.
            "dt_img/models/8/aaaa.png": (100, OLD),
        })

        stats, _ = _sweep(prune, fake, protected={7: {"aaaa"}})

        assert fake.deleted == ["dt_img/models/7/cccc.png", "dt_img/models/8/aaaa.png"]
        assert stats["protected"] == 2

    def test_dry_run_reports_but_deletes_nothing(self, prune):
        fake = FakeB2({"dt_img/models/7/aaaa.png": (100, OLD)})

        stats, lines = _sweep(prune, fake, apply=False)

        assert fake.batches == []
        assert stats["removed"] == 1 and len(lines) == 1

    def test_failed_keys_are_not_counted_as_freed(self, prune):
        bad = "dt_img/models/7/aaaa.png"
        fake = FakeB2({bad: (100, OLD), "dt_img/models/7/cccc.png": (1, OLD)},
                      fail={bad})

        stats, lines = _sweep(prune, fake)

        assert stats["removed"] == 1 and stats["failed"] == 1
        assert stats["freed"] == 1
        assert not any(bad in ln for ln in lines)

    def test_deletes_go_in_batches(self, prune, monkeypatch):
        monkeypatch.setattr(prune, "DELETE_CHUNK", 2)
        fake = FakeB2({f"dt_img/models/7/{i:04x}.png": (1, OLD) for i in range(5)})

        _sweep(prune, fake)

        assert [len(b) for b in fake.batches] == [2, 2, 1]

    def test_limit_bounds_a_run(self, prune):
        fake = FakeB2({f"dt_img/models/7/{i:04x}.png": (1, OLD) for i in range(5)})

        stats, _ = _sweep(prune, fake, limit=3)

        assert stats["removed"] == 3

    def test_foreign_layouts_are_ignored(self, prune):
        fake = FakeB2({
            "dt_img/models/not-a-player/aaaa.png": (1, OLD),
            "dt_img/models/7/deeper/aaaa.png": (1, OLD),
            "dt_img/user-upload/7/drop/x.png": (1, OLD),
        })

        stats, _ = _sweep(prune, fake)

        assert stats["scanned"] == 0 and fake.batches == []


class TestLocalSweep:
    def test_same_policy_over_the_filesystem(self, prune, tmp_path):
        root = tmp_path / "models"
        old_ts = time.time() - 20 * 86400
        for rel in ("7/aaaa.png", "7/aaaa-avatar.png", "7/aaaa.glb",
                    "7/cccc.png", "7/dddd.png", "x/eeee.png"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 10)
            if rel != "7/dddd.png":
                os.utime(p, (old_ts, old_ts))

        snap = _Snap()
        stats = prune.sweep_local(str(root), time.time() - 5 * 86400,
                                  {7: {"aaaa"}}, snap, apply=True)

        assert not (root / "7/cccc.png").exists()
        assert (root / "7/aaaa.png").exists(), "protected"
        assert (root / "7/aaaa-avatar.png").exists(), "protected crop"
        assert (root / "7/aaaa.glb").exists(), "models are never candidates"
        assert (root / "7/dddd.png").exists(), "inside the window"
        assert (root / "x/eeee.png").exists(), "not a player directory"
        assert stats["removed"] == 1 and stats["protected"] == 2

    def test_dry_run_leaves_the_tree_alone(self, prune, tmp_path):
        root = tmp_path / "models"
        p = root / "7/cccc.png"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x")
        old_ts = time.time() - 20 * 86400
        os.utime(p, (old_ts, old_ts))

        stats = prune.sweep_local(str(root), time.time() - 5 * 86400, {},
                                  _Snap(), apply=False)

        assert p.exists() and stats["removed"] == 1
