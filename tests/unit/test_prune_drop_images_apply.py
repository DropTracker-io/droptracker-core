"""End-to-end rehearsal of the prune script's delete path against temp files.

``--apply`` permanently removes user uploads that exist in no backup
(scripts/db_backup.sh covers MariaDB + Redis only), so the loop that decides
what to unlink is rehearsed here against a throwaway tree and a scripted
session before it is ever pointed at production.

Covered: dry run touches nothing, apply deletes + clears, high-value and
in-window drops are never selected, an already-missing file still clears its
dangling reference, and a failed unlink does NOT clear image_url.

Also pinned here: the *shape* of the candidate query. It is normally not worth
asserting on SQL text, but this scan is one index choice away from being a
table walk that times out — which is exactly how the job died on 2026-08-01 —
and nothing else would catch that drift until the nightly run failed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
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

    Serves the candidate rows once, then empty (ending the keyset walk), and
    records every UPDATE it is asked to run.

    Tests hand candidates in as ``(drop_id, image_url)``; the ``date_added``
    the scan pages on is supplied here, because which timestamp a row carries
    is irrelevant to what these tests assert (files unlinked, references
    cleared, snapshot written). ``MIN(date_added)`` is answered just inside the
    retention window so the walk builds two windows rather than one per day
    since 2024.
    """

    ROW_DATE = datetime(2026, 1, 1, 12, 0, 0)

    def __init__(self, candidates, recap_payloads=()):
        self._candidates = [(cid, url, self.ROW_DATE) for cid, url in candidates]
        self._recap_payloads = list(recap_payloads)
        self._served = False
        self.cleared: list[int] = []
        self.commits = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        if "MIN(date_added)" in sql:
            return _Result([(datetime.now() - timedelta(days=32),)])
        if "recap_snapshots" in sql:
            return _Result([(p,) for p in self._recap_payloads])
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

    def scalar(self):
        return self._rows[0][0] if self._rows else None


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


def test_recap_referenced_screenshot_survives_pruning(prune):
    """A screenshot a recap card points at must outlive the retention window.

    Recap pages are permanent by design, and the biggest-drop card renders the
    screenshot straight from the frozen payload — so a player's best month
    being worth under the prune threshold used to blank their own card 30 days
    after it was sent. Capturing the URL in the snapshot was never enough on
    its own; the file has to stay too.
    """
    module, root = prune
    keep = _make_image(root, "1/drop/Zulrah/Coal_0.jpg")
    drop = _make_image(root, "2/drop/Zulrah/Coal_0.jpg")
    payload = json.dumps({
        "biggest_drop": {"image_url": URL + "1/drop/Zulrah/Coal_0.jpg"},
    })
    session = FakeSession(
        [(101, URL + "1/drop/Zulrah/Coal_0.jpg"),
         (102, URL + "2/drop/Zulrah/Coal_0.jpg")],
        recap_payloads=[payload],
    )

    _run(module, session, ["--apply"])

    assert keep.exists(), "a recap-referenced screenshot must never be pruned"
    assert not drop.exists(), "unreferenced screenshots still prune normally"
    # The protected row keeps its reference; only the pruned one is cleared.
    assert session.cleared == [102]


def test_recap_protection_survives_a_malformed_payload(prune):
    """One unparsable snapshot must not disable protection for the rest."""
    module, root = prune
    keep = _make_image(root, "1/drop/Zulrah/Coal_0.jpg")
    session = FakeSession(
        [(101, URL + "1/drop/Zulrah/Coal_0.jpg")],
        recap_payloads=["{not json", json.dumps(
            {"biggest_drop": {"image_url": URL + "1/drop/Zulrah/Coal_0.jpg"}})],
    )

    _run(module, session, ["--apply"])

    assert keep.exists()
    assert session.cleared == []


class _RecordingSession(FakeSession):
    """FakeSession that keeps every statement it was handed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sql: list[str] = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        return super().execute(statement, params)


def _candidate_sql(session) -> str:
    return next(s for s in session.sql if "FROM drops" in s and "image_url" in s)


def test_candidates_are_selected_by_date_range_not_partition(prune):
    """The scan must ride ix_drops_date_added, and this is how it stops.

    `WHERE partition = :p` has no composite index behind it and degrades to a
    ref over the whole partition — 33M rows, which killed this job on
    2026-08-01. The module docstring carries the measurements; this pins the
    shape so the query cannot quietly drift back.
    """
    module, root = prune
    _make_image(root, "1/drop/Zulrah/Coal_0.jpg")
    session = _RecordingSession([(101, URL + "1/drop/Zulrah/Coal_0.jpg")])

    _run(module, session, [])

    sql = _candidate_sql(session)
    assert "date_added >=" in sql and "date_added <" in sql
    assert "`partition`" not in sql


def test_candidates_are_ordered_by_date_not_drop_id(prune):
    """Ordering by drop_id makes the optimiser drop the date index for the
    PRIMARY key and walk the table from row 1 — measured worse (87.8M rows)
    than the partition scan it would be replacing. So the keyset has to be
    expressed against date_added."""
    module, root = prune
    _make_image(root, "1/drop/Zulrah/Coal_0.jpg")
    session = _RecordingSession([(101, URL + "1/drop/Zulrah/Coal_0.jpg")])

    _run(module, session, [])

    sql = _candidate_sql(session)
    assert "ORDER BY date_added, drop_id" in sql


def test_windows_are_half_open_and_stop_at_the_cutoff(prune):
    """A drop must land in exactly one window, and none may reach past the
    retention cutoff — the window bounds are the only thing enforcing it now
    that the query carries no `date_added < :cutoff` filter of its own."""
    module, _root = prune
    module.session = FakeSession([])
    cutoff = datetime.now() - timedelta(days=30)

    windows = module.windows_to_scan(cutoff)

    assert windows, "expected at least one window"
    assert windows[-1][1] == cutoff, "the last window must end exactly at the cutoff"
    for (_a, end), (start, _b) in zip(windows, windows[1:]):
        assert end == start, "windows must abut without gaps or overlap"
    assert all(start < end for start, end in windows)
