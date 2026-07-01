"""Unit tests for utils/group_config.py."""

import time
from unittest.mock import MagicMock, patch

import pytest

import utils.group_config as gc


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_config_row(group_id: int, key: str, value: str):
    row = MagicMock()
    row.group_id = group_id
    row.config_key = key
    row.config_value = value
    return row


def _make_session(first_result=None, all_results=None):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = first_result
    session.query.return_value.filter.return_value.all.return_value = (
        all_results if all_results is not None else []
    )
    return session


# ── is_truthy ─────────────────────────────────────────────────────────────────

class TestIsTruthy:
    @pytest.mark.parametrize("val", ["1", "true", "True", "TRUE", " 1 ", " true "])
    def test_truthy_values(self, val):
        assert gc.is_truthy(val) is True

    @pytest.mark.parametrize("val", ["0", "false", "False", "off", "", "no", None])
    def test_falsy_values(self, val):
        assert gc.is_truthy(val) is False


# ── get ───────────────────────────────────────────────────────────────────────

class TestGet:
    def test_returns_value_from_db(self):
        row = _make_config_row(5, gc.MINIMUM_VALUE_TO_NOTIFY, "1000000")
        session = _make_session(first_result=row)
        result = gc.get(session, 5, gc.MINIMUM_VALUE_TO_NOTIFY)
        assert result == "1000000"

    def test_returns_default_when_not_in_db(self):
        session = _make_session(first_result=None)
        result = gc.get(session, 5, gc.MINIMUM_VALUE_TO_NOTIFY, default=2_500_000)
        assert result == 2_500_000

    def test_returns_none_when_absent_and_no_default(self):
        session = _make_session(first_result=None)
        assert gc.get(session, 5, gc.MINIMUM_VALUE_TO_NOTIFY) is None

    def test_caches_result(self):
        row = _make_config_row(5, gc.NOTIFY_PBS, "1")
        session = _make_session(first_result=row)
        gc.get(session, 5, gc.NOTIFY_PBS)
        gc.get(session, 5, gc.NOTIFY_PBS)
        # DB should only be queried once
        assert session.query.call_count == 1

    def test_cache_miss_after_expiry(self):
        row = _make_config_row(5, gc.NOTIFY_PBS, "1")
        session = _make_session(first_result=row)
        gc.get(session, 5, gc.NOTIFY_PBS)
        # Force expiry
        gc._cache[5][gc.NOTIFY_PBS] = (
            gc._cache[5][gc.NOTIFY_PBS][0],
            time.monotonic() - 1,
        )
        gc.get(session, 5, gc.NOTIFY_PBS)
        assert session.query.call_count == 2

    def test_caches_none_for_absent_key(self):
        session = _make_session(first_result=None)
        gc.get(session, 7, gc.NOTIFY_PETS)
        gc.get(session, 7, gc.NOTIFY_PETS)
        assert session.query.call_count == 1


# ── get_bulk ──────────────────────────────────────────────────────────────────

class TestGetBulk:
    def test_returns_values_from_db(self):
        rows = [
            _make_config_row(1, gc.NOTIFY_PBS, "1"),
            _make_config_row(2, gc.NOTIFY_PBS, "0"),
        ]
        session = _make_session(all_results=rows)
        result = gc.get_bulk(session, [1, 2], [gc.NOTIFY_PBS])
        assert result == {(1, gc.NOTIFY_PBS): "1", (2, gc.NOTIFY_PBS): "0"}

    def test_absent_keys_not_in_result(self):
        session = _make_session(all_results=[])
        result = gc.get_bulk(session, [1], [gc.NOTIFY_PBS])
        assert result == {}

    def test_uses_cache_for_hits(self):
        row = _make_config_row(3, gc.SPLIT_GP_TRACKING, "1")
        session = _make_session(all_results=[row])
        gc.get_bulk(session, [3], [gc.SPLIT_GP_TRACKING])
        # Second call — all cached
        session2 = _make_session(all_results=[])
        result2 = gc.get_bulk(session2, [3], [gc.SPLIT_GP_TRACKING])
        assert result2 == {(3, gc.SPLIT_GP_TRACKING): "1"}
        assert session2.query.call_count == 0

    def test_populates_cache_for_single_get(self):
        rows = [_make_config_row(4, gc.LOOT_BOARD_TYPE, "rounded")]
        session = _make_session(all_results=rows)
        gc.get_bulk(session, [4], [gc.LOOT_BOARD_TYPE])
        # Single get should hit cache, not DB
        session2 = _make_session(first_result=None)
        val = gc.get(session2, 4, gc.LOOT_BOARD_TYPE)
        assert val == "rounded"
        assert session2.query.call_count == 0

    def test_partial_cache_hit(self):
        # group 1 already cached, group 2 needs DB
        gc._cache[1] = {
            gc.NOTIFY_CLOGS: ("1", time.monotonic() + 30),
        }
        rows = [_make_config_row(2, gc.NOTIFY_CLOGS, "0")]
        session = _make_session(all_results=rows)
        result = gc.get_bulk(session, [1, 2], [gc.NOTIFY_CLOGS])
        assert result == {(1, gc.NOTIFY_CLOGS): "1", (2, gc.NOTIFY_CLOGS): "0"}
        # Only group 2 should have triggered a DB query
        assert session.query.call_count == 1


# ── invalidate ────────────────────────────────────────────────────────────────

class TestInvalidate:
    def test_invalidate_all(self):
        gc._cache[10] = {gc.NOTIFY_PBS: ("1", time.monotonic() + 30)}
        gc.invalidate(10)
        assert 10 not in gc._cache

    def test_invalidate_specific_key(self):
        gc._cache[10] = {
            gc.NOTIFY_PBS: ("1", time.monotonic() + 30),
            gc.NOTIFY_PETS: ("0", time.monotonic() + 30),
        }
        gc.invalidate(10, gc.NOTIFY_PBS)
        assert gc.NOTIFY_PBS not in gc._cache[10]
        assert gc.NOTIFY_PETS in gc._cache[10]

    def test_invalidate_nonexistent_group_is_noop(self):
        gc.invalidate(999)  # should not raise

    def test_invalidate_nonexistent_key_is_noop(self):
        gc._cache[10] = {gc.NOTIFY_PBS: ("1", time.monotonic() + 30)}
        gc.invalidate(10, gc.NOTIFY_CLOGS)  # key not present, should not raise
        assert gc.NOTIFY_PBS in gc._cache[10]


# ── Constants ─────────────────────────────────────────────────────────────────

class TestConstants:
    def test_known_keys_are_strings(self):
        for name in [
            "DROP_CHANNEL_ID", "MINIMUM_VALUE_TO_NOTIFY", "ONLY_SEND_MESSAGES_WITH_IMAGES",
            "SEND_STACKS_OF_ITEMS", "LOOTBOARD_CHANNEL_ID", "LOOTBOARD_MESSAGE_ID",
            "REPOST_LOOTBOARD", "SPLIT_GP_TRACKING", "LOOT_BOARD_TYPE",
            "NOTIFY_PBS", "NOTIFY_CLOGS", "NOTIFY_CAS", "MIN_CA_TIER_TO_NOTIFY",
            "NOTIFY_PETS", "NOTIFY_QUESTS",
        ]:
            assert isinstance(getattr(gc, name), str), f"{name} should be a str"
