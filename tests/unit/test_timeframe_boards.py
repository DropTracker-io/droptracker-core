"""Pure-logic tests for the custom-timeframe lootboard tiers
(lootboard/timeframe.py): range classification, day/month token generation,
item-hash parsing/merging, and path→URL mapping. The Redis/MySQL fetchers are
exercised in production verification; everything decision-shaped lives here.
"""
from datetime import date

import pytest

from lootboard.timeframe import (
    RangePlan,
    classify_range,
    daily_tokens,
    format_group_items,
    image_path_to_url,
    merge_item_hashes,
    month_partitions,
    parse_item_hash_value,
)

TODAY = date(2026, 7, 13)
RETENTION = 90


class TestDailyTokens:
    def test_inclusive_range(self):
        assert daily_tokens(date(2026, 7, 6), date(2026, 7, 8)) == [
            "20260706", "20260707", "20260708",
        ]

    def test_single_day(self):
        assert daily_tokens(date(2026, 7, 6), date(2026, 7, 6)) == ["20260706"]

    def test_month_boundary(self):
        assert daily_tokens(date(2026, 6, 30), date(2026, 7, 1)) == ["20260630", "20260701"]

    def test_inverted_is_empty(self):
        assert daily_tokens(date(2026, 7, 8), date(2026, 7, 6)) == []


class TestMonthPartitions:
    def test_within_one_month(self):
        assert month_partitions(date(2026, 7, 2), date(2026, 7, 30)) == [202607]

    def test_spans_year_end(self):
        assert month_partitions(date(2025, 11, 15), date(2026, 1, 10)) == [202511, 202512, 202601]


class TestParseItemHashValue:
    def test_plain_ints(self):
        assert parse_item_hash_value("3,1500000") == (3, 1500000)

    def test_five_field_format(self):
        raw = "2,400000,2,2026-07-06 10:00:00,2026-07-06 11:00:00"
        assert parse_item_hash_value(raw) == (2, 400000)

    def test_float_and_scientific(self):
        assert parse_item_hash_value(b"1,1.5e+07") == (1, 15000000)
        assert parse_item_hash_value("2.0,123.9") == (2, 123)

    @pytest.mark.parametrize("bad", [None, "", "justone", "x,y", b"\xff\xfe"])
    def test_garbage_is_none(self, bad):
        assert parse_item_hash_value(bad) is None


class TestMergeAndFormat:
    def test_merges_across_hashes_and_players(self):
        merged = merge_item_hashes([
            {b"4151": b"1,2000000"},
            {"4151": "2,4000000", "11832": "1,30000000"},
            {},
        ])
        assert merged["4151"] == (3, 6000000)
        assert merged["11832"] == (1, 30000000)

    def test_format_drops_zero_rows(self):
        out = format_group_items({"1": (0, 0), "2": (1, 5)})
        assert out == {"2": "1,5"}


class TestClassifyRange:
    def test_recent_range_uses_redis(self):
        plan = classify_range(date(2026, 7, 6), date(2026, 7, 12), today=TODAY, retention_days=RETENTION)
        assert plan == RangePlan("redis", date(2026, 7, 6), date(2026, 7, 12))

    def test_old_range_uses_hourly(self):
        plan = classify_range(date(2025, 3, 1), date(2025, 3, 31), today=TODAY, retention_days=RETENTION)
        assert plan.mode == "hourly"

    def test_range_straddling_retention_edge_uses_hourly(self):
        start = TODAY.replace(month=3, day=1)  # well before today-89d
        plan = classify_range(start, TODAY, today=TODAY, retention_days=RETENTION)
        assert plan.mode == "hourly"

    def test_future_end_rejected(self):
        with pytest.raises(ValueError):
            classify_range(date(2026, 7, 1), date(2026, 7, 14), today=TODAY)

    def test_inverted_rejected(self):
        with pytest.raises(ValueError):
            classify_range(date(2026, 7, 10), date(2026, 7, 1), today=TODAY)

    def test_before_epoch_rejected(self):
        with pytest.raises(ValueError):
            classify_range(date(2024, 9, 1), date(2024, 9, 30), today=TODAY)

    def test_over_a_year_rejected(self):
        with pytest.raises(ValueError):
            classify_range(date(2024, 11, 1), date(2026, 1, 1), today=TODAY)


class TestImagePathToUrl:
    def test_maps_static_img_tree(self):
        path = "/store/droptracker/disc/static/assets/img/clans/74/lb/202607060000-202607122359.png"
        assert image_path_to_url(path) == (
            "https://www.droptracker.io/img/clans/74/lb/202607060000-202607122359.png"
        )

    def test_rejects_foreign_paths(self):
        assert image_path_to_url("/etc/passwd") is None
        assert image_path_to_url("") is None
