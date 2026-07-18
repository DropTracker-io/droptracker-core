"""Unit tests for api/routes/group_export.py helpers (time parsing + window rules)."""

from datetime import datetime

import pytest

from api.routes.group_export import parse_time


class TestParseTime:
    def test_iso_8601_with_z_suffix(self):
        assert parse_time("2026-07-01T12:30:00Z") == datetime(2026, 7, 1, 12, 30, 0)

    def test_iso_8601_naive_treated_as_utc(self):
        assert parse_time("2026-07-01T12:30:00") == datetime(2026, 7, 1, 12, 30, 0)

    def test_iso_8601_date_only(self):
        assert parse_time("2026-07-01") == datetime(2026, 7, 1, 0, 0, 0)

    def test_iso_8601_with_offset_converted_to_utc(self):
        assert parse_time("2026-07-01T14:30:00+02:00") == datetime(2026, 7, 1, 12, 30, 0)

    def test_epoch_seconds(self):
        # 2026-07-01T00:00:00Z
        assert parse_time("1782864000") == datetime(2026, 7, 1, 0, 0, 0)

    def test_epoch_milliseconds(self):
        assert parse_time("1782864000000") == datetime(2026, 7, 1, 0, 0, 0)

    @pytest.mark.parametrize("bad", ["", None, "not-a-date", "2026-13-45", "12:30"])
    def test_invalid_values_return_none(self, bad):
        assert parse_time(bad) is None

    def test_whitespace_stripped(self):
        assert parse_time("  2026-07-01T00:00:00Z  ") == datetime(2026, 7, 1, 0, 0, 0)
