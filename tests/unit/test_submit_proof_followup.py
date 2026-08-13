"""Unit tests for data/submissions/manual_proof.py — the "ask for a screenshot
only when it matters" decision and the attach-after-the-fact path behind
/submit (t59).

commands/submissions.py itself can't be imported here (the conftest stubs
``interactions`` as a MagicMock, so Extension subclasses fail at class
creation), which is exactly why the decision + SQL live in the pure module
under test.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from data.submissions.manual_proof import (
    ACTIVE_EVENTS_KEY,
    AWAITING_PROOF_TTL_SECONDS,
    PROOF_MAX_BYTES,
    attach_proof_url,
    attached_summary_text,
    awaiting_proof_key,
    clear_awaiting_proof,
    events_active,
    event_names,
    format_names,
    latest_batch,
    load_awaiting_proof,
    pending_proof_rows,
    proof_attachment_error,
    proof_extension,
    proof_prompt_text,
    stash_awaiting_proof,
)


# ── Doubles ───────────────────────────────────────────────────────────────────

class FakeResult:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows


class FakeSession:
    """Records every statement + params, returns canned results."""

    def __init__(self, result=None):
        self.result = result if result is not None else FakeResult()
        self.calls = []
        self.commits = 0

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        return self.result

    def commit(self):
        self.commits += 1


class FakeRedis:
    def __init__(self, broken=False):
        self.store = {}
        self.ttls = {}
        self.broken = broken

    def _boom(self):
        if self.broken:
            raise RuntimeError("redis down")

    def setex(self, key, seconds, value):
        self._boom()
        self.store[key] = value
        self.ttls[key] = seconds

    def get(self, key):
        self._boom()
        return self.store.get(key)

    def delete(self, key):
        self._boom()
        self.store.pop(key, None)

    def exists(self, key):
        self._boom()
        return 1 if key in self.store else 0


_NOW = datetime(2026, 8, 12, 12, 0, 0)
_ROW = (7, 42, 3, "Summer Bingo", "Any Barrows unique", _NOW)
_FIELDS = ("id", "player_id", "event_id", "event_name", "task_label", "created_at")


def _row_dict(row=_ROW):
    return dict(zip(_FIELDS, row))


# ── events_active: the O(1) gate in front of everything else ──────────────────

def test_events_active_true_when_gate_key_present():
    conn = FakeRedis()
    conn.store[ACTIVE_EVENTS_KEY] = "1"
    assert events_active(conn) is True


def test_events_active_false_without_gate_key_or_redis():
    assert events_active(FakeRedis()) is False
    assert events_active(None) is False
    assert events_active(FakeRedis(broken=True)) is False


def test_active_events_key_matches_the_engine():
    """The gate literal is duplicated (importing the engine into the bot's
    submit path would drag in the whole events stack) — keep it honest."""
    source = (Path(__file__).resolve().parents[2] / "services" / "event_engine.py").read_text()
    match = re.search(r'^ACTIVE_EVENTS_KEY\s*=\s*"([^"]+)"', source, re.MULTILINE)
    assert match is not None
    assert match.group(1) == ACTIVE_EVENTS_KEY


# ── pending_proof_rows: the qualification read-back ───────────────────────────

def test_pending_rows_shape_and_params():
    s = FakeSession(FakeResult([_ROW]))
    before = datetime.now()
    rows = pending_proof_rows(s, [42], 180)
    assert rows == [{
        "id": 7, "player_id": 42, "event_id": 3,
        "event_name": "Summer Bingo", "task_label": "Any Barrows unique",
        "created_at": _NOW,
    }]
    sql, params = s.calls[0]
    assert params["player_ids"] == [42]
    assert params["limit"] == 25
    # Only rows younger than the lookback window count as "what they just sent".
    assert before - timedelta(seconds=181) <= params["since"] <= before - timedelta(seconds=179)
    # The verdict is the engine's: pending + no proof, nothing re-scored here.
    assert "status = 'pending'" in sql
    assert "proof_url IS NULL" in sql


def test_pending_rows_without_players_never_queries():
    s = FakeSession(FakeResult([_ROW]))
    assert pending_proof_rows(s, [], 180) == []
    assert pending_proof_rows(s, None, 180) == []
    assert s.calls == []


def test_pending_rows_empty_means_do_not_prompt():
    """No pending ledger row = the submission touched no event (or no policy
    held it) — the casual case that must stay silent."""
    s = FakeSession(FakeResult([]))
    assert pending_proof_rows(s, [42], 180) == []


def test_latest_batch_keeps_one_submissions_fan_out():
    """One submission can create several completions (several tasks, or one per
    team) within the same second — all of those belong to the screenshot."""
    rows = [
        _row_dict((9, 42, 3, "Summer Bingo", "Task B", _NOW)),
        _row_dict((8, 42, 3, "Summer Bingo", "Task A", _NOW - timedelta(seconds=1))),
        _row_dict((2, 42, 3, "Summer Bingo", "Older thing", _NOW - timedelta(minutes=40))),
    ]
    assert [r["id"] for r in latest_batch(rows)] == [9, 8]


def test_latest_batch_passes_rows_through_without_timestamps():
    rows = [{"id": 1, "created_at": None}]
    assert latest_batch(rows) == rows
    assert latest_batch([]) == []


# ── attach_proof_url ──────────────────────────────────────────────────────────

def test_attach_writes_and_commits():
    s = FakeSession(FakeResult(rowcount=2))
    assert attach_proof_url(s, [7, 8], [42], "https://www.droptracker.io/img/p.png") == 2
    sql, params = s.calls[0]
    assert params["completion_ids"] == [7, 8]
    assert params["player_ids"] == [42]
    # Ownership + status are re-applied in the UPDATE: a stale registry entry
    # must not be able to write onto someone else's or an actioned row.
    assert "player_id IN" in sql
    assert "status = 'pending'" in sql
    assert "proof_url IS NULL" in sql
    assert s.commits == 1


def test_attach_truncates_to_the_column_width():
    s = FakeSession(FakeResult(rowcount=1))
    attach_proof_url(s, [7], [42], "https://x/" + "a" * 400)
    assert len(s.calls[0][1]["proof_url"]) == 255


@pytest.mark.parametrize(
    "ids,players,url",
    [([], [42], "u"), ([7], [], "u"), ([7], [42], ""), (None, None, None)],
)
def test_attach_noop_when_nothing_to_write(ids, players, url):
    s = FakeSession(FakeResult(rowcount=1))
    assert attach_proof_url(s, ids, players, url) == 0
    assert s.calls == []
    assert s.commits == 0


# ── Awaiting-proof registry ───────────────────────────────────────────────────

def test_registry_round_trip():
    conn = FakeRedis()
    assert stash_awaiting_proof(
        conn, 123, player_id=42, player_name="Player One",
        completion_ids=[7, 8], channel_id=999, summary="**Bandos hilt**",
    ) is True
    assert conn.ttls[awaiting_proof_key(123)] == AWAITING_PROOF_TTL_SECONDS

    payload = load_awaiting_proof(conn, 123)
    assert payload["completion_ids"] == [7, 8]
    assert payload["player_id"] == 42
    assert payload["channel_id"] == "999"
    assert payload["summary"] == "**Bandos hilt**"

    clear_awaiting_proof(conn, 123)
    assert load_awaiting_proof(conn, 123) is None


def test_registry_reads_bytes_from_redis():
    """redis-py here is not decode_responses, so GET hands back bytes."""
    conn = FakeRedis()
    conn.store[awaiting_proof_key(5)] = json.dumps({"completion_ids": [1]}).encode()
    assert load_awaiting_proof(conn, 5) == {"completion_ids": [1]}


def test_registry_survives_garbage_and_outages():
    conn = FakeRedis()
    conn.store[awaiting_proof_key(5)] = "not json"
    assert load_awaiting_proof(conn, 5) is None
    conn.store[awaiting_proof_key(6)] = json.dumps([1, 2])  # not a dict
    assert load_awaiting_proof(conn, 6) is None
    assert load_awaiting_proof(None, 5) is None
    assert load_awaiting_proof(FakeRedis(broken=True), 5) is None
    assert stash_awaiting_proof(FakeRedis(broken=True), 1, player_id=1,
                                player_name="x", completion_ids=[1]) is False
    clear_awaiting_proof(FakeRedis(broken=True), 1)  # must not raise


def test_registry_needs_something_to_point_at():
    conn = FakeRedis()
    assert stash_awaiting_proof(conn, 1, player_id=1, player_name="x",
                                completion_ids=[]) is False
    assert conn.store == {}


# ── Attachment validation / naming ────────────────────────────────────────────

@pytest.mark.parametrize("ctype", ["image/png", "image/jpeg", "image/webp",
                                   "image/gif", "image/png; charset=binary", "IMAGE/PNG"])
def test_accepted_proof_types(ctype):
    assert proof_attachment_error(ctype, 1024) is None


@pytest.mark.parametrize("ctype", ["application/pdf", "video/mp4", "", None, "text/plain"])
def test_rejected_proof_types(ctype):
    assert "PNG" in proof_attachment_error(ctype, 1024)


def test_oversize_proof_rejected():
    assert "10 MB" in proof_attachment_error("image/png", PROOF_MAX_BYTES + 1)
    assert proof_attachment_error("image/png", PROOF_MAX_BYTES) is None


@pytest.mark.parametrize(
    "ctype,filename,expected",
    [
        ("image/png", "a.png", "png"),
        ("image/jpeg", "a.jpg", "jpg"),
        ("image/webp", None, "webp"),
        ("image/gif", None, "gif"),
        (None, "shot.JPEG", "jpg"),
        (None, "shot.webp", "webp"),
        (None, "shot.bin", "png"),
        (None, None, "png"),
    ],
)
def test_proof_extension(ctype, filename, expected):
    assert proof_extension(ctype, filename) == expected


# ── Copy ──────────────────────────────────────────────────────────────────────

def test_event_names_dedupes_in_order():
    rows = [{"event_name": "B"}, {"event_name": "A"}, {"event_name": "B"}, {"event_name": ""}]
    assert event_names(rows) == ["B", "A"]


def test_format_names_variants():
    assert format_names([]) == "an event"
    assert format_names(["A"]) == "**A**"
    assert format_names(["A", "B"]) == "**A** and **B**"
    assert format_names(["A", "B", "C"]) == "**A**, **B** and 1 other"
    assert format_names(["A", "B", "C", "D"]) == "**A**, **B** and 2 others"


def test_prompt_names_the_event_and_both_follow_up_routes():
    text = proof_prompt_text("**Bandos hilt** from **General Graardor**",
                             [_row_dict()], "</submit proof:12345>")
    assert "Summer Bingo" in text
    assert "Any Barrows unique" in text
    assert "</submit proof:12345>" in text
    assert "DM" in text
    assert "rejected" in text


def test_attached_summary_counts_and_handles_a_lost_race():
    rows = [_row_dict()]
    assert "1 pending submission" in attached_summary_text(rows, 1)
    assert "2 pending submissions" in attached_summary_text(rows, 2)
    # An admin got there first — the UPDATE matched nothing.
    assert "already reviewed" in attached_summary_text(rows, 0)
