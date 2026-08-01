"""Unit tests for recurring event activation schedules (web82a):
config validation, rule -> window materialization, and the read helpers.

Loaded directly from the file path (like test_event_notifications.py) so the
conftest sys.modules stubs for db/services never interfere — the module's
top-level imports are stdlib-only by design.
"""

import importlib.util
import os
import sys
from datetime import datetime

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_schedule.py",
)
_spec = importlib.util.spec_from_file_location("_event_schedule_under_test", _MODULE_PATH)
es = importlib.util.module_from_spec(_spec)
sys.modules["_event_schedule_under_test"] = es
_spec.loader.exec_module(es)


def _weekend(**rule):
    """The canonical case: Sat 00:00 -> Mon 00:00, i.e. the whole weekend."""
    base = {
        "type": "weekly",
        "windows": [{"start_dow": 5, "start_time": "00:00",
                     "end_dow": 0, "end_time": "00:00"}],
    }
    base.update(rule)
    return {"tz": "UTC", "rule": base}


# August 2026: the 1st is a Saturday, so the month's weekends open on the
# 1st, 8th, 15th, 22nd and 29th.
AUG_START = datetime(2026, 8, 1)
AUG_END = datetime(2026, 9, 1)


# ── validation ───────────────────────────────────────────────────────────────

class TestValidateConfig:
    def test_none_and_empty_mean_continuous(self):
        assert es.validate_config(None) is None
        assert es.validate_config({}) is None

    def test_weekly_normalizes_and_defaults(self):
        out = es.validate_config(_weekend())
        assert out["v"] == es.SCHEDULE_VERSION and out["tz"] == "UTC"
        assert out["rule"]["interval_weeks"] == 1
        assert out["rule"]["month_ordinal"] is None
        assert out["rule"]["windows"] == [
            {"start_dow": 5, "start_time": "00:00",
             "end_dow": 0, "end_time": "00:00"},
        ]

    def test_times_are_zero_padded(self):
        out = es.validate_config(_weekend(windows=[
            {"start_dow": 2, "start_time": "9:5",
             "end_dow": 2, "end_time": "17:0"}]))
        spec = out["rule"]["windows"][0]
        assert spec["start_time"] == "09:05" and spec["end_time"] == "17:00"

    @pytest.mark.parametrize("bad", [
        {"tz": "Europe/London", "rule": {"type": "daily",
                                         "start_time": "01:00", "end_time": "02:00"}},
        {"tz": "UTC", "rule": {"type": "nonsense"}},
        {"tz": "UTC", "rule": {"type": "weekly", "windows": []}},
        {"tz": "UTC", "rule": {"type": "daily",
                               "start_time": "25:00", "end_time": "02:00"}},
        {"tz": "UTC", "rule": {"type": "daily",
                               "start_time": "12:00", "end_time": "12:00"}},
        {"tz": "UTC", "rule": {"type": "custom", "windows": [{"start": 5, "end": 5}]}},
    ])
    def test_rejects_bad_configs(self, bad):
        with pytest.raises(es.ScheduleError):
            es.validate_config(bad)

    def test_interval_and_ordinal_are_mutually_exclusive(self):
        with pytest.raises(es.ScheduleError):
            es.validate_config(_weekend(interval_weeks=2, month_ordinal=1))

    def test_weekday_numbers_are_bounded(self):
        with pytest.raises(es.ScheduleError):
            es.validate_config(_weekend(windows=[
                {"start_dow": 7, "start_time": "00:00",
                 "end_dow": 0, "end_time": "00:00"}]))

    def test_custom_windows_are_sorted(self):
        out = es.validate_config({"tz": "UTC", "rule": {
            "type": "custom",
            "windows": [{"start": 300, "end": 400}, {"start": 100, "end": 200}],
        }})
        assert [w["start"] for w in out["rule"]["windows"]] == [100, 300]

    def test_parse_config_tolerates_corruption(self):
        # A corrupt stored config must read as "continuous", never raise: the
        # scoring gate simply stops narrowing rather than freezing an event.
        assert es.parse_config("{not json") is None
        assert es.parse_config("") is None
        assert es.parse_config('{"no": "rule"}') is None


# ── materialization ──────────────────────────────────────────────────────────

class TestWeekly:
    def test_weekends_of_a_month(self):
        windows = es.materialize(es.validate_config(_weekend()), AUG_START, AUG_END)
        assert len(windows) == 5
        assert windows[0] == (datetime(2026, 8, 1), datetime(2026, 8, 3))
        assert windows[-1] == (datetime(2026, 8, 29), datetime(2026, 8, 31))

    def test_clamped_to_the_event_span(self):
        # Starting mid-Saturday clips that first weekend rather than dropping it.
        windows = es.materialize(es.validate_config(_weekend()),
                                 datetime(2026, 8, 1, 18, 0), AUG_END)
        assert windows[0] == (datetime(2026, 8, 1, 18, 0), datetime(2026, 8, 3))

    def test_every_other_week_anchors_on_the_first_occurrence(self):
        windows = es.materialize(es.validate_config(_weekend(interval_weeks=2)),
                                 AUG_START, AUG_END)
        assert [w[0].day for w in windows] == [1, 15, 29]

    def test_month_ordinal_first(self):
        windows = es.materialize(es.validate_config(_weekend(month_ordinal=1)),
                                 AUG_START, datetime(2026, 10, 1))
        # First weekend of August, then of September (Sep 2026 starts Tuesday,
        # so its first Saturday is the 5th).
        assert [(w[0].month, w[0].day) for w in windows] == [(8, 1), (9, 5)]

    def test_month_ordinal_last(self):
        windows = es.materialize(es.validate_config(_weekend(month_ordinal=-1)),
                                 AUG_START, datetime(2026, 10, 1))
        assert [(w[0].month, w[0].day) for w in windows] == [(8, 29), (9, 26)]

    def test_multiple_specs_merge_when_they_touch(self):
        # Sat 00:00->Sun 00:00 plus Sun 00:00->Mon 00:00 is one weekend, not two
        # adjacent windows: a submission at the seam must not fall in a gap.
        config = es.validate_config(_weekend(windows=[
            {"start_dow": 5, "start_time": "00:00", "end_dow": 6, "end_time": "00:00"},
            {"start_dow": 6, "start_time": "00:00", "end_dow": 0, "end_time": "00:00"},
        ]))
        windows = es.materialize(config, AUG_START, AUG_END)
        assert windows[0] == (datetime(2026, 8, 1), datetime(2026, 8, 3))

    def test_single_evening_each_week(self):
        config = es.validate_config(_weekend(windows=[
            {"start_dow": 2, "start_time": "18:00",
             "end_dow": 2, "end_time": "23:59"}]))
        windows = es.materialize(config, AUG_START, AUG_END)
        # Wednesdays in August 2026: 5th, 12th, 19th, 26th.
        assert [w[0].day for w in windows] == [5, 12, 19, 26]
        assert windows[0] == (datetime(2026, 8, 5, 18, 0),
                              datetime(2026, 8, 5, 23, 59))


class TestDaily:
    def test_prime_time_every_evening(self):
        config = es.validate_config({"tz": "UTC", "rule": {
            "type": "daily", "start_time": "19:00", "end_time": "23:00"}})
        windows = es.materialize(config, datetime(2026, 8, 1), datetime(2026, 8, 5))
        assert len(windows) == 4
        assert windows[0] == (datetime(2026, 8, 1, 19), datetime(2026, 8, 1, 23))

    def test_window_crossing_midnight(self):
        config = es.validate_config({"tz": "UTC", "rule": {
            "type": "daily", "start_time": "22:00", "end_time": "02:00"}})
        windows = es.materialize(config, datetime(2026, 8, 1), datetime(2026, 8, 4))
        # The window that opened the evening BEFORE the event is clipped to the
        # event start, not dropped: 00:30 on the 1st really is inside a
        # 22:00->02:00 window, and nothing before the start ever leaks through.
        assert windows[0] == (datetime(2026, 8, 1), datetime(2026, 8, 1, 2))
        assert windows[1] == (datetime(2026, 8, 1, 22), datetime(2026, 8, 2, 2))
        assert all(w[0] >= datetime(2026, 8, 1) for w in windows)


class TestCustom:
    def test_explicit_windows_pass_through_clamped(self):
        a = int(datetime(2026, 8, 2, 12).timestamp())
        b = int(datetime(2026, 8, 2, 18).timestamp())
        config = es.validate_config({"tz": "UTC", "rule": {
            "type": "custom", "windows": [{"start": a, "end": b}]}})
        windows = es.materialize(config, AUG_START, AUG_END)
        assert windows == [(datetime(2026, 8, 2, 12), datetime(2026, 8, 2, 18))]


class TestMaterializeGuards:
    def test_requires_both_dates(self):
        with pytest.raises(es.ScheduleError):
            es.materialize(es.validate_config(_weekend()), AUG_START, None)

    def test_rejects_inverted_span(self):
        with pytest.raises(es.ScheduleError):
            es.materialize(es.validate_config(_weekend()), AUG_END, AUG_START)

    def test_schedule_that_never_opens_is_an_error(self):
        # Mon-Tue event with a weekends-only rule: better a clear 422 at save
        # time than an event that silently never scores.
        with pytest.raises(es.ScheduleError):
            es.materialize(es.validate_config(_weekend()),
                           datetime(2026, 8, 3), datetime(2026, 8, 5))

    def test_too_many_windows_is_an_error(self):
        config = es.validate_config({"tz": "UTC", "rule": {
            "type": "daily", "start_time": "01:00", "end_time": "02:00"}})
        with pytest.raises(es.ScheduleError):
            es.materialize(config, datetime(2026, 1, 1), datetime(2028, 1, 1))


# ── read helpers ─────────────────────────────────────────────────────────────

class TestReadHelpers:
    WINDOWS = [(datetime(2026, 8, 1), datetime(2026, 8, 3)),
               (datetime(2026, 8, 8), datetime(2026, 8, 10))]

    def test_containment_is_half_open(self):
        assert es.in_any_window(self.WINDOWS, datetime(2026, 8, 1))
        assert es.in_any_window(self.WINDOWS, datetime(2026, 8, 2, 12))
        # The closing instant belongs to the gap, not the window — so the same
        # timestamp can never count twice across adjacent windows.
        assert not es.in_any_window(self.WINDOWS, datetime(2026, 8, 3))
        assert not es.in_any_window(self.WINDOWS, datetime(2026, 8, 5))

    def test_no_schedule_is_always_open(self):
        assert es.in_any_window([], datetime(2026, 8, 5))
        assert es.in_any_window(None, datetime(2026, 8, 5))

    def test_current_and_next(self):
        mid = datetime(2026, 8, 2)
        assert es.current_window(self.WINDOWS, mid) == self.WINDOWS[0]
        assert es.next_window(self.WINDOWS, mid) == self.WINDOWS[1]

        gap = datetime(2026, 8, 5)
        assert es.current_window(self.WINDOWS, gap) is None
        assert es.next_window(self.WINDOWS, gap) == self.WINDOWS[1]

        after = datetime(2026, 8, 20)
        assert es.current_window(self.WINDOWS, after) is None
        assert es.next_window(self.WINDOWS, after) is None


class TestDescribe:
    def test_weekly_cadences(self):
        assert es.describe(_weekend()) == "Weekly: Sat 00:00 → Mon 00:00 UTC"
        assert es.describe(_weekend(interval_weeks=2)).endswith("every other week")
        assert es.describe(_weekend(month_ordinal=-1)).endswith(
            "the last occurrence each month")

    def test_daily_and_custom(self):
        assert es.describe({"rule": {"type": "daily", "start_time": "19:00",
                                     "end_time": "23:00"}}) == "Daily: 19:00 → 23:00 UTC"
        assert es.describe({"rule": {"type": "custom", "windows": [{}, {}]}}) == \
            "2 custom scoring windows"

    def test_no_schedule(self):
        assert es.describe(None) is None
