"""The group admin diagnostics panel (web111a).

The panel this replaces had three defects that all pointed the same way — it
looked like a working dashboard while telling an admin nothing:

1. Volume came from ``notified``, which only ever gets a row when a submission
   clears the announce threshold *and* reaches Discord, and which only stores
   drops at all. A clan tracking 20,000 drops a day charted "7 submissions".
2. ``members_synced_ts`` read a Redis key (``wom_sync_last:{wom_id}``) that no
   code in either repo ever wrote, so every group reported "never".
3. ``intake_healthy`` was ``MAX(date_added)`` over the whole ``drops`` table —
   it answered "is the site up", not "is this clan submitting".

The SQL-shaped helpers are exercised through a scripted session so the shaping
each one does (zero-fill, bounds, ordering, fallbacks) is pinned without a
database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import web_api.routes.group_admin as ga


# ── Fakes ────────────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        if not self._rows:
            return None
        row = self._rows[0]
        return row[0] if isinstance(row, tuple) else row


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _S:
    """Scripted session. ``execute`` returns the next scripted result set and
    records the bound parameters, so a test can assert on the window bounds the
    helper actually sent."""

    def __init__(self, *results, queries=()):
        self._results = list(results)
        self._queries = list(queries)
        self.params = []

    def execute(self, stmt, params=None):
        self.params.append(params or {})
        assert self._results, "helper issued more statements than the test scripted"
        return _Result(self._results.pop(0))

    def query(self, *a, **k):
        assert self._queries, "helper issued more ORM queries than the test scripted"
        return _Q(self._queries.pop(0))


# ── The `date_hour` bound ────────────────────────────────────────────────────

class TestHourKey:
    def test_uses_the_rollups_dashed_hour_format(self):
        # `player_npc_hourly_totals.date_hour` is 'YYYY-MM-DD-HH'
        # (services/npc_totals.py). An ISO 'YYYY-MM-DD 00' bound still compares
        # — a space sorts below '-' — so the window silently widened by a day
        # instead of failing. Pin the exact shape.
        assert ga._diag_hour_key(date(2026, 9, 9)) == "2026-09-09-00"

    def test_sorts_at_the_start_of_its_own_day(self):
        key = ga._diag_hour_key(date(2026, 9, 9))
        assert key <= "2026-09-09-00"
        assert key > "2026-09-08-23"


# ── Daily series ─────────────────────────────────────────────────────────────

class TestDailySeries:
    def test_zero_fills_the_whole_window(self):
        # A quiet clan must keep its x-axis: the chart used to collapse onto
        # whichever days happened to return rows, so a 3-day outage looked like
        # a 3-day-shorter month.
        start = date(2026, 9, 1)
        s = _S([("2026-09-02", 40, 900, 3)])
        out = ga._diag_daily(s, [1, 2], start, 5)
        assert [r["date"] for r in out] == [
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
            "2026-09-04",
            "2026-09-05",
        ]
        assert out[1] == {"date": "2026-09-02", "drops": 40, "gp": 900, "players": 3}
        assert out[0]["drops"] == 0 and out[4]["gp"] == 0

    def test_ignores_rows_outside_the_window(self):
        # The bound is a `>=` on a string column, so a stray older row must not
        # land anywhere in the output rather than creating a bucket.
        s = _S([("2026-08-30", 99, 99, 9), ("2026-09-01", 5, 5, 1)])
        out = ga._diag_daily(s, [1], date(2026, 9, 1), 2)
        assert len(out) == 2
        assert sum(r["drops"] for r in out) == 5

    def test_empty_roster_skips_the_query_entirely(self):
        # `IN ()` is a syntax error, and a group with nobody in it is a real
        # state (a freshly created group).
        s = _S()  # no scripted results: a query here would assert
        out = ga._diag_daily(s, [], date(2026, 9, 1), 3)
        assert len(out) == 3
        assert all(r["drops"] == 0 for r in out)


# ── Hour-of-day matrix ───────────────────────────────────────────────────────

class TestHourMatrix:
    def test_is_seven_by_twentyfour_monday_first(self):
        # WEEKDAY() is 0=Monday (DAYOFWEEK() is 1=Sunday — using the wrong one
        # rotates the whole heatmap by a day).
        s = _S([(0, 13, 500), (6, 23, 7)])
        m = ga._diag_hour_matrix(s, [1], date(2026, 9, 1))
        assert len(m) == 7 and all(len(row) == 24 for row in m)
        assert m[0][13] == 500
        assert m[6][23] == 7
        assert m[3][4] == 0

    def test_drops_out_of_range_buckets(self):
        s = _S([(9, 0, 1), (0, 99, 1), (None, 3, 1)])
        m = ga._diag_hour_matrix(s, [1], date(2026, 9, 1))
        assert sum(sum(row) for row in m) == 0

    def test_empty_roster_returns_an_empty_matrix(self):
        m = ga._diag_hour_matrix(_S(), [], date(2026, 9, 1))
        assert m == [[0] * 24 for _ in range(7)]


# ── Coverage ─────────────────────────────────────────────────────────────────

class TestCoverage:
    def test_reuses_the_seven_day_count_for_a_seven_day_window(self):
        # active_7d / active_30d / active_window are three counts of the same
        # shape; when the requested window is one of them the helper must not
        # pay for a fourth identical query.
        today = date(2026, 9, 9)
        s = _S((80,), (92,), (104,))  # 7d, 30d, ever
        out = ga._diag_coverage(s, [1, 2, 3], today, today - timedelta(days=6))
        assert out["active_7d"] == 80
        assert out["active_30d"] == 92
        assert out["active_window"] == 80
        assert out["tracked_ever"] == 104
        assert out["roster"] == 3

    def test_a_ninety_day_window_pays_for_its_own_count(self):
        today = date(2026, 9, 9)
        s = _S((80,), (92,), (97,), (104,))
        out = ga._diag_coverage(s, [1], today, today - timedelta(days=89))
        assert out["active_window"] == 97

    def test_empty_roster_short_circuits(self):
        out = ga._diag_coverage(_S(), [], date(2026, 9, 9), date(2026, 9, 3))
        assert out == {
            "roster": 0,
            "active_7d": 0,
            "active_30d": 0,
            "active_window": 0,
            "tracked_ever": 0,
        }


# ── Named lists respect the display opt-out ──────────────────────────────────

class TestTopPlayers:
    def test_hidden_players_are_not_named(self):
        s = _S([(1, "Visible", 10, 2)])
        out = ga._diag_top_players(s, [1, 2], {2}, date(2026, 9, 1))
        assert [p["player_id"] for p in out] == [1]
        # The opt-out is applied to the id list, not to the returned rows —
        # otherwise the LIMIT would silently return fewer than it should.
        assert s.params[0]["pids"] == [1]

    def test_an_all_hidden_roster_returns_nothing_without_querying(self):
        assert ga._diag_top_players(_S(), [1, 2], {1, 2}, date(2026, 9, 1)) == []


# ── Members-synced timestamp ─────────────────────────────────────────────────

class TestMembersSynced:
    def _group(self, updated=None):
        return SimpleNamespace(group_id=7, date_updated=updated)

    def test_prefers_the_sync_marker(self):
        stamp = datetime(2026, 9, 9, 7, 45)
        s = _S(queries=[[(stamp.isoformat(),)]])
        assert ga._diag_members_synced_ts(s, self._group(datetime(2020, 1, 1))) == int(
            stamp.timestamp()
        )

    def test_falls_back_to_the_group_row_before_the_first_stamped_sync(self):
        # Every group predates the marker, so the panel has to show something
        # until `_sync_group_from_wom` writes one — `groups.date_updated` is
        # touched by that same function and is the closest thing available.
        touched = datetime(2026, 9, 9, 7, 45)
        s = _S(queries=[[]])
        assert ga._diag_members_synced_ts(s, self._group(touched)) == int(touched.timestamp())

    def test_a_malformed_marker_falls_back_rather_than_raising(self):
        touched = datetime(2026, 9, 9, 7, 45)
        s = _S(queries=[[("not-a-timestamp",)]])
        assert ga._diag_members_synced_ts(s, self._group(touched)) == int(touched.timestamp())

    def test_never_synced_group_reports_nothing(self):
        s = _S(queries=[[]])
        assert ga._diag_members_synced_ts(s, self._group(None)) is None


# ── Warnings ─────────────────────────────────────────────────────────────────

class TestWarnings:
    def _coverage(self, **over):
        base = {"roster": 100, "active_7d": 12, "active_30d": 20}
        base.update(over)
        return base

    def _group(self, **over):
        base = {"group_id": 7, "guild_id": "123", "wom_id": 456}
        base.update(over)
        return SimpleNamespace(**base)

    def test_a_healthy_group_has_none(self):
        s = _S(queries=[[SimpleNamespace(config_value="999")]])
        assert ga._diag_warnings(s, self._group(), self._coverage(), 1_700_000_000, False) == []

    def test_missing_drops_channel_is_flagged(self):
        s = _S(queries=[[]])
        out = ga._diag_warnings(s, self._group(), self._coverage(), 1_700_000_000, False)
        assert any("channel_id_to_post_loot" in w for w in out)

    def test_a_blank_channel_value_counts_as_missing(self):
        s = _S(queries=[[SimpleNamespace(config_value="")]])
        out = ga._diag_warnings(s, self._group(), self._coverage(), 1_700_000_000, False)
        assert any("channel_id_to_post_loot" in w for w in out)

    def test_no_wom_link_is_flagged(self):
        s = _S(queries=[[SimpleNamespace(config_value="9")]])
        out = ga._diag_warnings(s, self._group(wom_id=None), self._coverage(), 1, False)
        assert any("WiseOldMan" in w for w in out)

    def test_nothing_ever_tracked_beats_the_quiet_week_warning(self):
        # A group nobody has installed the plugin in, and a group whose members
        # went quiet, need different answers.
        s = _S(queries=[[SimpleNamespace(config_value="9")]])
        out = ga._diag_warnings(s, self._group(), self._coverage(active_7d=0), None, False)
        assert any("nobody has the plugin" in w for w in out)
        assert not any("last 7 days" in w for w in out)

    def test_a_quiet_week_is_flagged_when_history_exists(self):
        s = _S(queries=[[SimpleNamespace(config_value="9")]])
        out = ga._diag_warnings(s, self._group(), self._coverage(active_7d=0), 1_700_000_000, False)
        assert any("last 7 days" in w for w in out)

    def test_an_oversized_roster_explains_the_blank_panels(self):
        # Group 2 holds every tracked player; the volume queries are skipped for
        # it, and without this the panel reads as a dead 24,000-member clan.
        s = _S(queries=[[SimpleNamespace(config_value="9")]])
        out = ga._diag_warnings(s, self._group(), self._coverage(roster=24352, active_7d=0), None, True)
        assert any("24,352 members" in w for w in out)
        assert not any("nobody has the plugin" in w for w in out)
