"""Recap period arithmetic and the annual fold (services/recap.py).

Everything here is the pure half of the module — the parts that decide *which*
rows to read and how to combine them. The SQL half needs a live DB and lives in
the integration suite.

The fold is the piece most worth pinning down: it is what makes an annual recap
affordable (one player-year against ``drops`` measures 6.1s, twelve stored rows
measure milliseconds), so a regression there is a performance cliff rather than
a visible bug.

Loaded from the file path (like test_badges_leaders.py) so the conftest
``services`` stub doesn't shadow the real module; its own ``db`` import resolves
to the conftest MagicMock, which is fine — nothing here touches the ORM.
"""

import importlib.util
import os
import sys
from datetime import datetime

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "recap.py",
)
_spec = importlib.util.spec_from_file_location("_recap_under_test", _MODULE_PATH)
recap = importlib.util.module_from_spec(_spec)
sys.modules["_recap_under_test"] = recap
_spec.loader.exec_module(recap)


class TestPeriodTokens:
    def test_month_period_zero_pads(self):
        assert recap.month_period(202601) == "2026-01"
        assert recap.month_period(202612) == "2026-12"

    def test_period_partition_roundtrips(self):
        for partition in (202410, 202601, 202607, 202612):
            assert recap.period_partition(recap.month_period(partition)) == partition

    def test_year_and_month_are_distinguishable_by_shape(self):
        assert recap.is_month_period("2026-07")
        assert not recap.is_year_period("2026-07")
        assert recap.is_year_period("2026")
        assert not recap.is_month_period("2026")

    def test_period_partition_rejects_a_year(self):
        with pytest.raises(ValueError):
            recap.period_partition("2026")

    def test_periods_sort_chronologically_as_strings(self):
        # The whole reason for the 'YYYY-MM' shape: no parsing needed to order
        # them, in SQL or in Python.
        months = ["2026-01", "2025-12", "2026-10", "2026-02"]
        assert sorted(months) == ["2025-12", "2026-01", "2026-02", "2026-10"]

    def test_year_months_is_twelve_in_order(self):
        months = recap.year_months(2025)
        assert len(months) == 12
        assert months[0] == "2025-01" and months[-1] == "2025-12"
        assert months == sorted(months)


class TestBounds:
    def test_month_bounds_are_half_open(self):
        # Half-open so a drop at 23:59:59 on the 31st lands in exactly one
        # period — a BETWEEN would double-count the boundary instant.
        start, end = recap.month_bounds("2026-01")
        assert start == "2026-01-01 00:00:00"
        assert end == "2026-02-01 00:00:00"

    def test_month_bounds_roll_the_year(self):
        start, end = recap.month_bounds("2026-12")
        assert start == "2026-12-01 00:00:00"
        assert end == "2027-01-01 00:00:00"

    def test_hour_bounds_are_inclusive_and_reach_the_last_hour(self):
        # date_hour is an hour bucket, not an instant, so the final bucket of
        # the month has to be included rather than excluded.
        lo, hi = recap.month_hour_bounds("2026-06")
        assert lo == "2026-06-01-00"
        assert hi == "2026-06-30-23"  # June has 30 days

    def test_hour_bounds_respect_month_length(self):
        assert recap.month_hour_bounds("2026-02")[1] == "2026-02-28-23"
        assert recap.month_hour_bounds("2024-02")[1] == "2024-02-29-23"  # leap
        assert recap.month_hour_bounds("2026-01")[1] == "2026-01-31-23"

    def test_hour_bounds_sort_lexicographically(self):
        # BETWEEN on a VARCHAR only works chronologically because every field
        # is zero-padded; guard that property directly.
        lo, hi = recap.month_hour_bounds("2026-06")
        assert lo < "2026-06-15-09" < hi

    def test_previous_month_wraps_the_year(self):
        assert recap.previous_month_period("2026-07") == "2026-06"
        assert recap.previous_month_period("2026-01") == "2025-12"


class TestPeriodClosed:
    def test_past_month_is_closed(self):
        assert recap.period_closed("2026-06", now=datetime(2026, 7, 15))

    def test_current_month_is_not_closed(self):
        # Publishing a partial month is the one mistake a recap can't walk back.
        assert not recap.period_closed("2026-07", now=datetime(2026, 7, 15))

    def test_future_month_is_not_closed(self):
        assert not recap.period_closed("2026-09", now=datetime(2026, 7, 15))

    def test_month_closes_the_instant_the_next_one_starts(self):
        assert recap.period_closed("2026-06", now=datetime(2026, 7, 1, 0, 0, 0))

    def test_year_closes_only_after_it_ends(self):
        assert recap.period_closed("2025", now=datetime(2026, 7, 15))
        assert not recap.period_closed("2026", now=datetime(2026, 12, 31))
        assert recap.period_closed("2026", now=datetime(2027, 1, 1))


def _month(period, loot, drops, *, npc_available=True, top_npcs=None,
           top_items=None, achievements=None, biggest=None, by_hour=None):
    """A minimal stored monthly payload, shaped like `_base_month_payload`."""
    return {
        "period": period,
        "subject": {"id": 14, "name": "Test Clan"},
        "totals": {"loot": loot, "drops": drops, "loot_rollup": loot},
        "top_items": top_items or [],
        "top_npcs": top_npcs or [],
        "npc_data_available": npc_available,
        "activity": {"by_hour": by_hour or [0] * 24, "by_weekday": [0] * 7},
        "achievements": achievements or {},
        "biggest_drop": biggest,
    }


class TestAnnualFold:
    """`compute_year` is exercised through a stubbed `load_snapshot` so the fold
    logic is tested without a database."""

    def _fold(self, monkeypatch, months_by_period):
        monkeypatch.setattr(
            recap, "load_snapshot",
            lambda session, scope, subject_id, period: months_by_period.get(period),
        )
        return recap.compute_year(None, "group", 14, 2025)

    def test_returns_none_when_no_months_are_stored(self, monkeypatch):
        assert self._fold(monkeypatch, {}) is None

    def test_sums_totals_across_stored_months(self, monkeypatch):
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 100, 10),
            "2025-02": _month("2025-02", 250, 25),
        })
        assert out["totals"]["loot"] == 350
        assert out["totals"]["drops"] == 35
        assert out["months_covered"] == ["2025-01", "2025-02"]

    def test_tolerates_a_partial_year(self, monkeypatch):
        # Tracked data starts 2024-10, and any month can be missing; folding
        # what exists beats refusing to produce a card.
        out = self._fold(monkeypatch, {"2025-03": _month("2025-03", 5, 5)})
        assert out["totals"]["loot"] == 5
        assert out["months_covered"] == ["2025-03"]

    def test_peak_month_is_the_biggest_looting_month(self, monkeypatch):
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 100, 1),
            "2025-09": _month("2025-09", 900, 1),
            "2025-12": _month("2025-12", 300, 1),
        })
        assert out["peak_month"] == {"period": "2025-09", "loot": 900}

    def test_biggest_drop_wins_across_the_whole_year(self, monkeypatch):
        small = {"item_name": "Dragon dagger", "total_value": 30_000}
        huge = {"item_name": "3rd age bow", "total_value": 2_089_744_999}
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 1, 1, biggest=small),
            "2025-02": _month("2025-02", 1, 1, biggest=huge),
            "2025-03": _month("2025-03", 1, 1, biggest=None),
        })
        assert out["biggest_drop"]["item_name"] == "3rd age bow"

    def test_biggest_drop_is_none_when_no_month_had_one(self, monkeypatch):
        out = self._fold(monkeypatch, {"2025-01": _month("2025-01", 1, 1)})
        assert out["biggest_drop"] is None

    def test_npc_entries_merge_and_kills_sum(self, monkeypatch):
        tob = lambda loot, kills: [
            {"npc_id": 1, "name": "Theatre of Blood", "loot": loot,
             "drops": 1, "kills": kills}
        ]
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 1, 1, top_npcs=tob(100, 5)),
            "2025-02": _month("2025-02", 1, 1, top_npcs=tob(200, 7)),
        })
        assert len(out["top_npcs"]) == 1
        assert out["top_npcs"][0]["loot"] == 300
        assert out["top_npcs"][0]["kills"] == 12

    def test_achievements_accumulate(self, monkeypatch):
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 1, 1, achievements={"pbs": 3, "cas": 1}),
            "2025-02": _month("2025-02", 1, 1, achievements={"pbs": 4}),
        })
        assert out["achievements"] == {"pbs": 7, "cas": 1}

    def test_activity_histograms_accumulate_by_slot(self, monkeypatch):
        a, b = [0] * 24, [0] * 24
        a[22], b[22], b[3] = 10, 5, 2
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 1, 1, by_hour=a),
            "2025-02": _month("2025-02", 1, 1, by_hour=b),
        })
        assert out["activity"]["by_hour"][22] == 15
        assert out["activity"]["by_hour"][3] == 2

    def test_npc_coverage_requires_every_folded_month(self, monkeypatch):
        # player_npc_hourly_totals is missing 202509-202606 entirely, so a year
        # spanning the gap must not present its boss totals as complete.
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 1, 1, npc_available=True),
            "2025-02": _month("2025-02", 1, 1, npc_available=True),
            "2025-10": _month("2025-10", 1, 1, npc_available=False),
        })
        assert out["npc_data_available"] is False
        assert out["npc_months_covered"] == 2

    def test_npc_coverage_true_when_every_month_has_it(self, monkeypatch):
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 1, 1, npc_available=True),
            "2025-02": _month("2025-02", 1, 1, npc_available=True),
        })
        assert out["npc_data_available"] is True
        assert out["npc_months_covered"] == 2

    def test_fold_stamps_the_period_and_schema_version(self, monkeypatch):
        out = self._fold(monkeypatch, {"2025-01": _month("2025-01", 1, 1)})
        assert out["period"] == "2025"
        assert out["schema_version"] == recap.RECAP_SCHEMA_VERSION
        assert out["generated_at"]
