"""B2 half of the screenshot retention (scripts/prune_drop_images).

New uploads land in the bucket (utils/download.py B2 mode), so every branch
of the prune has a B2 twin: value-weighed drops delete the object and clear
the row, referenced non-drop types are deleted row-by-row in the heal walk,
and the reference-less types are swept from a bucket listing. What these
tests pin is the *safety envelope*: only ``dt_img/user-upload/`` keys are
ever deletable, recap-protected keys survive, unknown URLs stay untouched,
and no credentials means no action at all.

URL→key parsing is the REAL ``utils.image_storage`` logic (its boto3 layer is
conftest-stubbed); the bucket IO is a fake recorded per test.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CDN = "https://video.droptracker.io/"


class FakeB2:
    """Real key parsing, scripted IO."""

    def __init__(self, objects=None):
        from utils import image_storage

        self.USER_UPLOAD_PREFIX = image_storage.USER_UPLOAD_PREFIX
        self.key_from_url = image_storage.key_from_url
        self.objects = dict(objects or {})  # key -> (size, last_modified)
        self.deleted: list[str] = []

    def head(self, key):
        if key not in self.objects:
            return None
        return {"size": self.objects[key][0], "etag": "e"}

    def delete_key(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)
        return True

    def list_keys(self, prefix):
        for key, (size, lm) in sorted(self.objects.items()):
            if key.startswith(prefix):
                yield {"key": key, "size": size, "etag": "e",
                       "last_modified": lm}


@pytest.fixture
def prune(tmp_path, monkeypatch):
    monkeypatch.setenv("B2_CDN_BASE_URL", "https://video.droptracker.io")
    spec = importlib.util.spec_from_file_location(
        "_prune_b2_under_test", REPO_ROOT / "scripts" / "prune_drop_images.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LOCAL_ROOT", str(tmp_path / "user-upload") + "/")
    monkeypatch.setattr(module, "REPO_ROOT", str(tmp_path))
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


class TestKeyResolution:
    def test_user_upload_keys_resolve(self, prune, monkeypatch):
        fake = FakeB2()
        monkeypatch.setattr(prune, "_b2_storage", lambda: fake)
        assert prune.b2_key_for(CDN + "dt_img/user-upload/1/drop/x.jpg") == \
            "dt_img/user-upload/1/drop/x.jpg"

    def test_other_namespaces_never_resolve(self, prune, monkeypatch):
        # A models key or a video must never become deletable from the
        # screenshot prune, whatever a row claims.
        fake = FakeB2()
        monkeypatch.setattr(prune, "_b2_storage", lambda: fake)
        assert prune.b2_key_for(CDN + "dt_img/models/1/ab.glb") is None
        assert prune.b2_key_for(CDN + "videos/1/clip.mp4") is None
        assert prune.b2_key_for(
            "https://www.droptracker.io/img/user-upload/1/drop/x.jpg") is None

    def test_no_credentials_means_no_action(self, prune, monkeypatch):
        monkeypatch.delenv("B2_KEY_ID", raising=False)
        assert prune._b2_storage() is None
        assert prune.b2_key_for(CDN + "dt_img/user-upload/1/drop/x.jpg") is None


class TestHealWalksB2Rows:
    """The keyset walk over referencing tables deletes aged B2 objects."""

    class _Session:
        def __init__(self, rows_by_table):
            self.rows_by_table = rows_by_table
            self.updates: list[list[int]] = []

        def execute(self, statement, params=None):
            sql = str(statement)

            class R:
                def __init__(self, rows):
                    self._rows = rows

                def all(self):
                    return self._rows

            if sql.strip().upper().startswith("UPDATE"):
                self.updates.append(list(params["ids"]))
                return R([])
            for table, rows in self.rows_by_table.items():
                if f"FROM {table} " in sql:
                    served = [r for r in rows if r[0] > params["last_pk"]]
                    return R(served)
            return R([])

        def commit(self):
            pass

    def test_b2_rows_are_deleted_and_cleared(self, prune, monkeypatch, tmp_path):
        fake = FakeB2({
            "dt_img/user-upload/9/pb/Zulrah/pb_1_ab12cd34.jpg":
                (100, datetime.now(timezone.utc) - timedelta(days=60)),
        })
        monkeypatch.setattr(prune, "_b2_storage", lambda: fake)
        session = self._Session({
            "personal_best": [
                (1, CDN + "dt_img/user-upload/9/pb/Zulrah/pb_1_ab12cd34.jpg"),
                (2, "https://elsewhere.example/whatever.jpg"),
            ],
        })
        monkeypatch.setattr(prune, "session", session)

        snap = (tmp_path / "snap.tsv").open("w")
        cleared = prune.heal_missing_references(
            datetime.now() - timedelta(days=30), apply=True,
            protected_b2=frozenset(), snap=snap)
        snap.close()

        assert fake.deleted == [
            "dt_img/user-upload/9/pb/Zulrah/pb_1_ab12cd34.jpg"]
        assert [1] in session.updates          # B2 row cleared
        assert cleared == 1                    # foreign URL row untouched

    def test_protected_b2_rows_survive(self, prune, monkeypatch, tmp_path):
        key = "dt_img/user-upload/9/pb/Zulrah/pb_1_ab12cd34.jpg"
        fake = FakeB2({key: (100, datetime.now(timezone.utc))})
        monkeypatch.setattr(prune, "_b2_storage", lambda: fake)
        session = self._Session({"personal_best": [(1, CDN + key)]})
        monkeypatch.setattr(prune, "session", session)

        snap = (tmp_path / "snap.tsv").open("w")
        cleared = prune.heal_missing_references(
            datetime.now() - timedelta(days=30), apply=True,
            protected_b2=frozenset({key}), snap=snap)
        snap.close()

        assert fake.deleted == []
        assert session.updates == []
        assert cleared == 0


class TestReflessSweep:
    def test_only_refless_types_age_out(self, prune, monkeypatch, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=45)
        new = datetime.now(timezone.utc) - timedelta(days=2)
        fake = FakeB2({
            "dt_img/user-upload/9/level_up/Mining/lvl_1_aa.jpg": (10, old),
            "dt_img/user-upload/9/level_up/Mining/lvl_2_bb.jpg": (10, new),
            "dt_img/user-upload/9/pet/Boss/pet_1_cc.jpg": (10, old),
            # Referenced types are the heal walk's job, never the sweep's:
            "dt_img/user-upload/9/pb/Zulrah/pb_1_dd.jpg": (10, old),
            "dt_img/user-upload/9/drop/Zulrah/drop_1_ee.jpg": (10, old),
            # Unknown layouts are left alone entirely.
            "dt_img/user-upload/9/proof/proof_1_ff.jpg": (10, old),
        })
        monkeypatch.setattr(prune, "_b2_storage", lambda: fake)

        snap = (tmp_path / "snap.tsv").open("w")
        totals = prune.prune_b2_refless_images(
            30, frozenset(), snap, apply=True)
        snap.close()

        assert sorted(fake.deleted) == [
            "dt_img/user-upload/9/level_up/Mining/lvl_1_aa.jpg",
            "dt_img/user-upload/9/pet/Boss/pet_1_cc.jpg",
        ]
        assert totals["level_up"][0] == 1
        assert totals["pet"][0] == 1

    def test_dry_run_deletes_nothing_but_reports(self, prune, monkeypatch,
                                                 tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=45)
        fake = FakeB2({
            "dt_img/user-upload/9/level_up/Mining/lvl_1_aa.jpg": (10, old)})
        monkeypatch.setattr(prune, "_b2_storage", lambda: fake)

        snap_path = tmp_path / "snap.tsv"
        with snap_path.open("w") as snap:
            totals = prune.prune_b2_refless_images(
                30, frozenset(), snap, apply=False)

        assert fake.deleted == []
        assert totals["level_up"][0] == 1
        assert "deleted_b2_level_up" in snap_path.read_text()

    def test_recap_protected_keys_survive(self, prune, monkeypatch, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=45)
        key = "dt_img/user-upload/9/level_up/Mining/lvl_1_aa.jpg"
        fake = FakeB2({key: (10, old)})
        monkeypatch.setattr(prune, "_b2_storage", lambda: fake)

        with (tmp_path / "snap.tsv").open("w") as snap:
            totals = prune.prune_b2_refless_images(
                30, frozenset({key}), snap, apply=True)

        assert fake.deleted == []
        assert totals["level_up"][2] == 1  # counted as protected
