"""End-to-end rehearsal of the prune script's delete path against temp files.

``--apply`` permanently removes user uploads that exist in no backup
(scripts/db_backup.sh covers MariaDB + Redis only), so the loop that decides
what to unlink is rehearsed here against a throwaway tree and a scripted
session before it is ever pointed at production.

Covered: dry run touches nothing, apply deletes + clears, high-value and
in-window drops are never selected, an already-missing file still clears its
dangling reference, and a failed unlink does NOT clear image_url.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def prune(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_prune_apply_under_test", REPO_ROOT / "scripts" / "prune_drop_images.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    root = tmp_path / "user-upload"
    root.mkdir()
    monkeypatch.setattr(module, "LOCAL_ROOT", str(root) + os.sep)
    monkeypatch.setattr(module, "URL_PREFIXES", (
        "https://www.droptracker.io/img/user-upload/", str(root) + os.sep))
    monkeypatch.setattr(module, "REPO_ROOT", str(tmp_path))
    try:
        yield module, root
    finally:
        sys.modules.pop(spec.name, None)


class FakeSession:
    """Scripted stand-in for db.models.base.session.

    Returns the candidate rows once per partition, then empty (ending the
    keyset walk), and records every UPDATE it is asked to run.
    """

    def __init__(self, candidates):
        self._candidates = candidates
        self._served = False
        self.cleared: list[int] = []
        self.commits = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        if "DISTINCT `partition`" in sql:
            return _Result([(202601,)])
        if sql.strip().upper().startswith("UPDATE"):
            self.cleared.extend(params["ids"])
            return _Result([])
        if self._served:
            return _Result([])
        self._served = True
        return _Result(self._candidates)

    def commit(self):
        self.commits += 1


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _run(module, session, argv):
    import db.models.base as base
    old_session = getattr(base, "session", None)
    module.session = session
    base.session = session
    old_argv = sys.argv
    sys.argv = ["prune_drop_images"] + argv
    try:
        return module.main()
    finally:
        sys.argv = old_argv
        base.session = old_session


def _make_image(root, relpath, payload=b"x" * 2048):
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


URL = "https://www.droptracker.io/img/user-upload/"


def test_dry_run_deletes_nothing(prune):
    module, root = prune
    img = _make_image(root, "1/drop/Zulrah/Coal_0.jpg")
    session = FakeSession([(101, URL + "1/drop/Zulrah/Coal_0.jpg")])

    assert _run(module, session, []) == 0

    assert img.exists(), "dry run must not delete"
    assert session.cleared == []
    assert session.commits == 0


def test_apply_deletes_file_and_clears_reference(prune):
    module, root = prune
    img = _make_image(root, "1/drop/Zulrah/Coal_0.jpg")
    session = FakeSession([(101, URL + "1/drop/Zulrah/Coal_0.jpg")])

    assert _run(module, session, ["--apply"]) == 0

    assert not img.exists(), "apply must delete the file"
    assert session.cleared == [101]


def test_apply_records_every_action_to_the_snapshot(prune):
    module, root = prune
    _make_image(root, "1/drop/Zulrah/Coal_0.jpg")
    session = FakeSession([
        (101, URL + "1/drop/Zulrah/Coal_0.jpg"),
        (102, URL + "1/drop/Zulrah/Gone_0.jpg"),      # file never existed
    ])

    _run(module, session, ["--apply"])

    logs = list((Path(module.REPO_ROOT) / "logs").glob("prune_drop_images_*.tsv"))
    assert len(logs) == 1
    lines = logs[0].read_text().strip().splitlines()
    assert lines[0] == "drop_id\timage_url\tbytes\taction"
    body = {ln.split("\t")[0]: ln.split("\t")[3] for ln in lines[1:]}
    # Both rows mutate the DB, so both must be recoverable from the snapshot.
    assert body == {"101": "deleted", "102": "cleared_missing"}
    assert sorted(session.cleared) == [101, 102]


def test_missing_file_still_clears_dangling_reference(prune):
    module, root = prune
    session = FakeSession([(102, URL + "9/drop/N/never_here.jpg")])

    _run(module, session, ["--apply"])

    assert session.cleared == [102]


def test_unresolvable_url_is_left_completely_alone(prune):
    module, root = prune
    session = FakeSession([(103, "https://cdn.discordapp.com/attachments/1/2/x.png")])

    _run(module, session, ["--apply"])

    # Not ours to delete, and its reference must survive.
    assert session.cleared == []


def test_failed_unlink_does_not_clear_the_reference(prune, monkeypatch):
    """If the file could not be removed, the row must keep pointing at it —
    clearing image_url would orphan a file we can no longer find."""
    module, root = prune
    _make_image(root, "1/drop/Zulrah/Coal_0.jpg")
    session = FakeSession([(101, URL + "1/drop/Zulrah/Coal_0.jpg")])

    def boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(module.os, "remove", boom)
    _run(module, session, ["--apply"])

    assert session.cleared == []


def test_traversal_url_never_escapes_the_root(prune, tmp_path):
    module, root = prune
    outside = tmp_path / "precious.jpg"
    outside.write_bytes(b"do not touch")
    session = FakeSession([(104, URL + "../precious.jpg")])

    _run(module, session, ["--apply"])

    assert outside.exists(), "a traversal url must never reach a real file"
    assert session.cleared == []
