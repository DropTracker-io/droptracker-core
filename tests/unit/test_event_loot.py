"""Unit tests for the pure event-window loot GP helpers
(web_api/event_loot.py). No DB/Redis — the window math and cache-coverage
logic are what matter; the rollup query is exercised against prod data."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from web_api.event_loot import covers, event_window

NOW = datetime(2026, 7, 22, 12, 0, 0)


def _ev(**kw):
    base = {"status": "active", "activated_at": None, "starts_at": None,
            "ends_at": None, "ended_at": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_window_uses_activation_and_clamps_end_to_now():
    ev = _ev(activated_at=datetime(2026, 7, 1), ends_at=datetime(2026, 8, 1))
    assert event_window(ev, NOW) == (datetime(2026, 7, 1), NOW)


def test_window_ended_event_uses_ended_at():
    ev = _ev(status="past", activated_at=datetime(2026, 7, 1),
             ended_at=datetime(2026, 7, 10))
    assert event_window(ev, NOW) == (datetime(2026, 7, 1), datetime(2026, 7, 10))


def test_window_draft_has_no_window_even_with_past_start():
    # A draft never ran — starts_at alone must not open a window.
    ev = _ev(status="draft", starts_at=datetime(2026, 7, 1))
    assert event_window(ev, NOW) is None


def test_window_legacy_non_draft_falls_back_to_starts_at():
    ev = _ev(status="past", starts_at=datetime(2026, 7, 1),
             ended_at=datetime(2026, 7, 5))
    assert event_window(ev, NOW) == (datetime(2026, 7, 1), datetime(2026, 7, 5))


def test_window_future_or_inverted_is_none():
    assert event_window(_ev(activated_at=datetime(2026, 8, 1)), NOW) is None
    assert event_window(
        _ev(activated_at=datetime(2026, 7, 10), ended_at=datetime(2026, 7, 5)), NOW
    ) is None
    assert event_window(_ev(), NOW) is None


def test_covers_requires_every_requested_pid():
    cached = {"1": 100, "2": 0}
    assert covers(cached, [1, 2])
    assert covers(cached, [2])
    assert not covers(cached, [1, 3])   # 3 not computed yet (mid-TTL join)
    assert not covers(None, [1])
    assert covers({}, [])


def test_hour_range_formats_zero_padded_hours():
    from web_api.event_loot import hour_range

    lo, hi = hour_range((datetime(2026, 7, 1, 4, 59), datetime(2026, 7, 22, 9, 0)))
    assert (lo, hi) == ("2026-07-01-04", "2026-07-22-09")
    assert lo < hi  # lexicographic == chronological
