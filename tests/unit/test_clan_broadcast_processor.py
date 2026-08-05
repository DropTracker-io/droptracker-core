"""Unit tests for data/submissions/clan_broadcast.py helper logic.

Follows the house pattern (see test_drop.py): the DB/services layers are
conftest-stubbed, so these tests exercise the pure/near-pure decision helpers
— guid determinism, clan-name binding, notification gating — not the full
session-bound processor flow (that's integration territory).
"""
from datetime import datetime

import pytest

from data.submissions.clan_broadcast import (
    GUID_BUCKET_SECONDS,
    _bound_group_ids,
    _clan_slug,
    _deterministic_guid,
    _notification_gate,
)


# ── deterministic guid ──────────────────────────────────────────────────────

def test_guid_is_deterministic_across_relayers():
    stamp = datetime(2026, 8, 5, 12, 0, 30)
    a = _deterministic_guid("my clan", "Alice received a drop: Twisted bow.", stamp)
    b = _deterministic_guid("my clan", "Alice received a drop: Twisted bow.", stamp)
    assert a == b
    assert a.startswith("cc:")
    assert len(a) == 3 + 64  # fits Drop.unique_id String(255)


def test_guid_same_bucket_same_guid():
    base = datetime(2026, 8, 5, 12, 0, 0)
    later = datetime.fromtimestamp(base.timestamp() + GUID_BUCKET_SECONDS - 1)
    assert _deterministic_guid("c", "m", base) == _deterministic_guid("c", "m", later)


def test_guid_differs_by_clan_message_and_bucket():
    stamp = datetime(2026, 8, 5, 12, 0, 0)
    next_bucket = datetime.fromtimestamp(stamp.timestamp() + GUID_BUCKET_SECONDS)
    base = _deterministic_guid("clan a", "msg", stamp)
    assert _deterministic_guid("clan b", "msg", stamp) != base
    assert _deterministic_guid("clan a", "other msg", stamp) != base
    assert _deterministic_guid("clan a", "msg", next_bucket) != base


# ── clan name normalization ─────────────────────────────────────────────────

def test_clan_slug_folds_case_spacing_and_markup():
    assert _clan_slug("The Best Clan") == _clan_slug("the best clan")
    assert _clan_slug("The Best Clan") == _clan_slug("The Best Clan")
    assert _clan_slug("The_Best_Clan") == _clan_slug("The Best Clan")
    assert _clan_slug("<img=3>The Best Clan") == _clan_slug("The Best Clan")
    assert _clan_slug("") == ""
    assert _clan_slug(None) == ""


# ── notification gate (mirrors drop_processor criteria) ─────────────────────

def _values(gid, min_value=None, send_stacks=None):
    values = {}
    if min_value is not None:
        values[(gid, "minimum_value_to_notify")] = min_value
    if send_stacks is not None:
        values[(gid, "send_stacks_of_items")] = send_stacks
    return values


def test_gate_unit_value_meets_minimum():
    assert _notification_gate(5, _values(5, "1000000"), 1_500_000, 1_500_000)
    assert not _notification_gate(5, _values(5, "2000000"), 1_500_000, 1_500_000)


def test_gate_stack_total_requires_send_stacks():
    # 200 x 50k = 10M stack: only announces when send_stacks is on.
    assert not _notification_gate(5, _values(5, "1000000"), 50_000, 10_000_000)
    assert _notification_gate(5, _values(5, "1000000", "1"), 50_000, 10_000_000)


def test_gate_defaults_to_2500000_on_missing_or_garbage_min():
    assert not _notification_gate(5, {}, 2_499_999, 2_499_999)
    assert _notification_gate(5, {}, 2_500_000, 2_500_000)
    assert _notification_gate(5, _values(5, "not-a-number"), 2_500_000, 2_500_000)


# ── group binding ───────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, group_ids):
        self._group_ids = group_ids

    def execute(self, *_args, **_kwargs):
        return _FakeResult([(gid,) for gid in self._group_ids])


class _FakePlayer:
    player_id = 42


def test_bound_groups_require_tracking_flag_and_clan_name_match(monkeypatch):
    from utils import group_config as gc

    config = {
        (10, "clan_broadcast_tracking"): "1",
        (10, "clan_chat_name"): "The Best Clan",
        (11, "clan_broadcast_tracking"): "1",
        (11, "clan_chat_name"): "Some Other Clan",
        (12, "clan_broadcast_tracking"): "0",   # opted out
        (12, "clan_chat_name"): "The Best Clan",
        (13, "clan_broadcast_tracking"): "1",   # opted in, name never set
        (10, "clan_broadcast_min_value"): "500000",
    }
    monkeypatch.setattr(gc, "get_bulk", lambda _s, _g, _k: config)

    bound = _bound_group_ids(
        _FakeSession([10, 11, 12, 13]), _FakePlayer(), _clan_slug("the_best_clan")
    )
    assert bound == {10: 500_000}


def test_bound_groups_skip_system_groups_and_handle_no_candidates(monkeypatch):
    from utils import group_config as gc

    monkeypatch.setattr(
        gc, "get_bulk",
        lambda _s, _g, _k: {
            (2, "clan_broadcast_tracking"): "1",
            (2, "clan_chat_name"): "Global",
        },
    )
    # Global group (2) and template (1) are never bindable.
    assert _bound_group_ids(_FakeSession([1, 2]), _FakePlayer(), _clan_slug("Global")) == {}
    assert _bound_group_ids(_FakeSession([]), _FakePlayer(), _clan_slug("x")) == {}


def test_bound_groups_bad_floor_defaults_to_zero(monkeypatch):
    from utils import group_config as gc

    monkeypatch.setattr(
        gc, "get_bulk",
        lambda _s, _g, _k: {
            (10, "clan_broadcast_tracking"): "true",
            (10, "clan_chat_name"): "Clan",
            (10, "clan_broadcast_min_value"): "banana",
        },
    )
    assert _bound_group_ids(_FakeSession([10]), _FakePlayer(), _clan_slug("Clan")) == {10: 0}
