"""Duplicate flagging in scripts/ticket_dossier.py (pure logic, no DB)."""
import importlib.util
import os
from datetime import datetime, timedelta

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "ticket_dossier.py")
_spec = importlib.util.spec_from_file_location("ticket_dossier_under_test", _PATH)
td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(td)

T0 = datetime(2026, 10, 18, 12, 0, 0)


def _drop(drop_id, item_id, seconds, value=1000, qty=1, name=None):
    return {"drop_id": drop_id, "item_id": item_id, "value": value, "quantity": qty,
            "item_name": name, "date_added": T0 + timedelta(seconds=seconds)}


def test_same_content_inside_window_flags_both_rows(monkeypatch):
    rows = [_drop(11, 5, 3), _drop(10, 5, 0), _drop(9, 7, 1)]
    monkeypatch.setattr(td, "_q", lambda *a, **k: rows)
    out = {r["drop_id"]: r for r in td._drops_for(None, 1, 50, None, 10)}
    assert out[10]["flags"] == ["DUP-ROW~11"]
    assert out[11]["flags"] == ["DUP-ROW~10"]
    assert "flags" not in out[9]


def test_noted_and_unnoted_ids_with_one_name_still_pair(monkeypatch):
    # Ticket #491: plugin row id 452 (noted), clan-chat copy id 451, values differ.
    rows = [_drop(2, 451, 3, value=10015, qty=27, name="Runite ore"),
            _drop(1, 452, 0, value=10020, qty=27, name="Runite ore")]
    monkeypatch.setattr(td, "_q", lambda *a, **k: rows)
    out = {r["drop_id"]: r for r in td._drops_for(None, 1, 50, None, 10)}
    assert out[1]["flags"] == ["DUP-ROW~2"] and out[2]["flags"] == ["DUP-ROW~1"]


def test_outside_window_or_different_quantity_is_not_flagged(monkeypatch):
    rows = [_drop(2, 5, 30), _drop(1, 5, 0), _drop(3, 5, 31, qty=2)]
    monkeypatch.setattr(td, "_q", lambda *a, **k: rows)
    assert not any("flags" in r for r in td._drops_for(None, 1, 50, None, 10))


def test_since_filters_before_flagging(monkeypatch):
    rows = [_drop(2, 5, 3), _drop(1, 5, 0)]
    monkeypatch.setattr(td, "_q", lambda *a, **k: rows)
    out = td._drops_for(None, 1, 50, T0 + timedelta(seconds=1), 10)
    assert [r["drop_id"] for r in out] == [2] and "flags" not in out[0]


def test_retry_fingerprint_flagged(monkeypatch):
    rows = [{"error_message": "transient, retrying: SSL"}, {"error_message": None}]
    monkeypatch.setattr(td, "_q", lambda *a, **k: rows)
    out = td._queue_for(None, 1, 10)
    assert out[0]["flags"] == ["RETRIED"] and "flags" not in out[1]


def test_attachment_paths_resolve_under_img_root():
    out = td._attachments('[{"filename": "a.png", "path": "tickets/491/a.png"}]')
    assert out[0]["local_path"].endswith(os.path.join("static", "assets", "img", "tickets", "491", "a.png"))
    assert td._attachments(None) == [] and td._attachments("not json") == [{"raw": "not json"}]
