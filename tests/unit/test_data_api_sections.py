"""Section parsing and the cost model.

These are the two pieces that decide what work a request is allowed to cause,
so they are worth pinning independently of any database. The loaders
themselves need real rows and belong to integration coverage.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sect = _load("_real_sections", "data_api/sections.py")


class TestParseInclude:
    def test_empty_gives_the_default(self):
        assert sect.parse_include("") == list(sect.DEFAULT_SECTIONS)
        assert sect.parse_include(None) == list(sect.DEFAULT_SECTIONS)

    def test_identity_is_always_present(self):
        # Every response needs to say who it is about, so identity is added
        # even when the caller asks only for loot.
        assert "identity" in sect.parse_include("loot")

    def test_order_and_dedupe_are_stable(self):
        assert sect.parse_include("loot,stats,loot") == ["identity", "loot", "stats"]

    def test_whitespace_and_case_are_forgiving(self):
        assert sect.parse_include("  LOOT , Stats ") == ["identity", "loot", "stats"]

    def test_all_expands_to_every_profile_section(self):
        # `all` is "everything about a player". The drop feed (and its
        # modifier) is an event stream with its own window and is opted out.
        expected = {k for k in sect.ALL_SECTION_KEYS if sect.REGISTRY[k].in_all}
        assert set(sect.parse_include("all")) == expected

    def test_unknown_section_is_an_error_not_a_silent_drop(self):
        # Silently ignoring a typo hands back a response missing the data the
        # caller asked for, which they may not notice.
        with pytest.raises(ValueError) as excinfo:
            sect.parse_include("loot,statz")
        assert excinfo.value.args[0] == "statz"


class TestCostModel:
    def test_cost_scales_with_players(self):
        one = sect.cost_of(["loot_items"], 1)
        hundred = sect.cost_of(["loot_items"], 100)
        assert hundred == one * 100

    def test_cost_adds_across_sections(self):
        assert (sect.cost_of(["loot", "loot_items"], 1)
                == sect.cost_of(["loot"], 1) + sect.cost_of(["loot_items"], 1))

    def test_identity_is_free(self):
        assert sect.REGISTRY["identity"].cost == 0

    def test_expensive_sections_cost_more_than_cheap_ones(self):
        # The ordering is the whole point: a rollup aggregation must not be
        # priced the same as a batched Redis GET.
        assert sect.REGISTRY["clog_slots"].cost > sect.REGISTRY["loot_items"].cost
        assert sect.REGISTRY["loot_items"].cost > sect.REGISTRY["clog"].cost
        assert sect.REGISTRY["clog"].cost > sect.REGISTRY["loot"].cost

    def test_cost_is_never_zero(self):
        # An identity-only page still consumes a connection and a query.
        assert sect.cost_of(["identity"], 1) >= 1
        assert sect.cost_of([], 0) >= 1

    def test_a_full_400_member_sweep_fits_the_entry_tier(self):
        # The sizing case the budgets were set from: a clan pulling everything
        # about a 400-member roster. The page cap makes that 4 requests, and
        # the whole sweep has to fit inside one minute on 'standard'
        # (200,000 cost units) with room to spare — a budget that only just
        # clears its target case 429s on the first retry, which is exactly what
        # happened when it was sized at 150,000. See dapi2_recalibrate_tiers.
        sweep = sect.cost_of(sect.parse_include("all"), 100) * 4
        assert sweep <= 200_000 * 0.75, sweep

    def test_the_two_heavy_sections_dominate_the_price(self):
        # Measured: clog_slots and loot_items are ~475x and ~357x the cheapest
        # section. If a change ever makes them comparable to the rest, the
        # weights have drifted from the measurement and need re-taking.
        # Over the *profile* sections (what `all` expands to): the drop feed is
        # a bounded event stream priced by window and deliberately outside it.
        heavy = sect.REGISTRY["clog_slots"].cost + sect.REGISTRY["loot_items"].cost
        everything = sum(sect.REGISTRY[k].cost for k in sect.ALL_SECTION_KEYS
                         if sect.REGISTRY[k].in_all)
        assert heavy > everything * 0.7, (heavy, everything)


class TestTimestampCoercion:
    """MySQL zero-dates arrive as strings, not datetimes."""

    def test_datetime_becomes_iso(self):
        from datetime import datetime

        assert sect._iso(datetime(2026, 8, 27, 12, 30)) == "2026-08-27T12:30:00"

    def test_none_stays_none(self):
        assert sect._iso(None) is None

    def test_zero_date_is_no_timestamp_not_a_crash(self):
        # '0000-00-00 00:00:00' is not a representable moment, so the driver
        # returns the raw string; 2,459 player_state rows carry it. Calling
        # .isoformat() on that raised and took the whole section down.
        assert sect._iso("0000-00-00 00:00:00") is None
        assert sect._iso("0000-00-00") is None

    def test_other_strings_pass_through(self):
        assert sect._iso("2026-08-27 12:30:00") == "2026-08-27 12:30:00"


class TestRegistryIntegrity:
    def test_every_section_has_a_loader_and_a_description(self):
        for key in sect.ALL_SECTION_KEYS:
            section = sect.REGISTRY[key]
            assert callable(section.loader), key
            assert section.description.strip(), key
            assert section.cost >= 0, key

    def test_the_only_read_of_the_drops_table_is_the_index_forced_feed(self):
        # drops is 207M rows with exactly one safe access shape: player_id IN
        # + a date_added range, with the composite index forced. The loot
        # aggregates read the rollups instead. The drop *feed* is the single
        # sanctioned read and must use exactly that shape — every FROM drops
        # in the loaders has to carry the hint, and there must be only one.
        # Matches the table in FROM/JOIN position only — 'SUM(drop_count) AS
        # drops' is an alias, not a read of the table.
        import re

        source = (_ROOT / "data_api" / "sections.py").read_text()
        body = source.split("# ── loaders", 1)[1]
        reads = re.findall(r"\b(?:FROM|JOIN)\s+drops\b[^\n]*", body, re.IGNORECASE)
        assert len(reads) == 1, reads
        assert "FORCE INDEX (ix_drops_player_id_date_added)" in reads[0] \
            or "_DROPS_INDEX_HINT" in reads[0], reads[0]
        # ...and the loader that owns it ranges on date_added, both ends.
        feed = body.split("def _load_drops", 1)[1].split("\ndef ", 1)[0]
        assert "d.date_added >= :start" in feed and "d.date_added < :end" in feed
        assert "ROW_NUMBER() OVER" in feed, "per-player cap must be applied in the query"

    def test_no_loader_filters_a_rollup_by_partition_equality(self):
        # partition is not the leading column of any usable rollup index, so an
        # equality there degrades to a player-only scan (measured at 4.9s vs
        # 56ms for the date range). Ranges on date_hour are the safe shape.
        import re

        source = (_ROOT / "data_api" / "sections.py").read_text()
        body = source.split("# ── loaders", 1)[1]
        assert not re.search(r"partition\s*=\s*:", body, re.IGNORECASE)


class TestClogSlotsShape:
    """The compact collection-log encoding.

    61% of slots have quantity 1, so the payload repeats that key hundreds of
    thousands of times in the naive shape. The compact form is an id array
    plus a sparse map, and "absent means 1" is the contract consumers rely on.
    """

    def _rows(self):
        # (player_id, item_id, quantity) as the driver returns them.
        return [(7, 100, 1), (7, 200, 5), (7, 300, 1), (9, 100, 1)]

    def _load(self, rows):
        class _Session:
            def execute(self, *_a, **_k):
                return rows

        return sect._load_clog_slots(_Session(), [7, 9], {})

    def test_items_are_a_plain_sorted_id_array(self):
        out = self._load(self._rows())
        assert out[7]["items"] == [100, 200, 300]
        assert out[9]["items"] == [100]

    def test_only_quantities_other_than_one_are_carried(self):
        out = self._load(self._rows())
        # 200 had quantity 5; the two quantity-1 slots are absent entirely.
        assert out[7]["quantities"] == {"200": 5}
        assert out[9]["quantities"] == {}

    def test_a_zero_quantity_is_recorded_not_dropped(self):
        # Absent means 1, so a genuine 0 has to be stated explicitly or it
        # would read back as 1.
        out = self._load([(7, 100, 0)])
        assert out[7]["quantities"] == {"100": 0}


class TestMetaSection:
    """Board standing, advertised as free — so it must actually be batched."""

    def test_meta_is_free(self):
        # The owner's requirement: attachable at no cost to the caller.
        assert sect.REGISTRY["meta"].cost == 0

    def test_meta_does_not_change_the_price_of_a_request(self):
        without = sect.cost_of(["identity", "loot"], 100)
        with_meta = sect.cost_of(["identity", "loot", "meta"], 100)
        assert with_meta == without

    def test_meta_loader_batches_rather_than_looping_per_player(self):
        # A per-player loop would be 400 Redis round trips on a big roster,
        # which is what web_api.common.player_list_loot_sum does and why it is
        # not reused. Pin the shape: one query, one batched rank call.
        import inspect

        source = inspect.getsource(sect._load_meta)
        assert "player_ranks(player_ids" in source, "ranks must be fetched for the whole page"
        assert source.count("session.execute") == 1, "should be one query, not one per player"

    def test_meta_never_reads_the_naive_sum_helper(self):
        from pathlib import Path

        source = (_ROOT / "data_api" / "group_meta.py").read_text()
        # player_list_loot_sum is a sequential loop; player_month_totals pipelines.
        assert "player_list_loot_sum" not in source.split('"""', 2)[-1]
        assert "player_month_totals" in source


class TestDropsFeed:
    """The individual-drop section: bounded, index-forced, opt-in ids."""

    def _rows(self):
        from datetime import datetime

        # (player_id, drop_id, item_id, item_name, npc_id, npc_name, qty, value, date_added)
        return [
            (7, 501, 4151, "Abyssal whip", 415, "Abyssal Sire", 1, 2_000_000,
             datetime(2026, 9, 2, 10, 0, 0)),
            (7, 500, 995, "Coins", 415, "Abyssal Sire", 1500, 1,
             datetime(2026, 9, 2, 9, 59, 30)),
            (9, 480, 11832, "Bandos chestplate", 2215, "General Graardor", 1, 20_000_000,
             datetime(2026, 9, 1, 22, 0, 0)),
        ]

    def _ctx(self, sections=("identity", "drops"), per_player=50):
        from datetime import datetime

        return {"sections": list(sections),
                "drops_window": (datetime(2026, 9, 1, 12, 0, 0), datetime(2026, 9, 2, 12, 0, 0)),
                "drops_per_player": per_player}

    def _load(self, rows, ctx):
        class _Session:
            def execute(self, *_a, **_k):
                return rows

        return sect._load_drops(_Session(), [7, 9, 11], ctx)

    def test_every_drop_carries_unix_seconds_utc(self):
        out = self._load(self._rows(), self._ctx())
        whip = out[7]["drops"][0]
        # 2026-09-02T10:00:00Z, computed as UTC regardless of the process zone.
        assert whip["received_at"] == 1788343200
        assert whip["received_at_iso"] == "2026-09-02T10:00:00"
        assert whip["total_value"] == 2_000_000 and whip["value_each"] == 2_000_000

    def test_drop_id_is_only_attached_with_the_modifier(self):
        without = self._load(self._rows(), self._ctx())
        assert "drop_id" not in without[7]["drops"][0]
        with_ids = self._load(self._rows(), self._ctx(("identity", "drops", "drop_ids")))
        assert with_ids[7]["drops"][0]["drop_id"] == 501

    def test_a_player_with_nothing_in_the_window_is_an_empty_feed_not_missing(self):
        out = self._load(self._rows(), self._ctx())
        assert out[11]["count"] == 0 and out[11]["drops"] == []
        assert out[11]["truncated"] is False and out[11]["oldest"] is None

    def test_window_bounds_are_echoed_as_unix(self):
        out = self._load(self._rows(), self._ctx())
        assert out[7]["from"] == 1788264000 and out[7]["to"] == 1788350400

    def test_hitting_the_per_player_cap_is_reported_with_the_oldest_seen(self):
        out = self._load(self._rows(), self._ctx(per_player=2))
        assert out[7]["truncated"] is True
        assert out[7]["oldest"] == out[7]["drops"][-1]["received_at"]
        assert out[9]["truncated"] is False

    def test_the_modifier_never_runs_and_never_emits_a_key(self):
        calls = []

        def _loader(_s, ids, _ctx):
            calls.append("drops")
            return {pid: {"count": 0} for pid in ids}

        original = sect.REGISTRY["drops"]
        sect.REGISTRY["drops"] = original._replace(loader=_loader)
        try:
            merged = sect.load_sections(None, ["identity", "drops", "drop_ids"], [7],
                                        self._ctx(("identity", "drops", "drop_ids")))
        finally:
            sect.REGISTRY["drops"] = original
        assert "drop_ids" not in merged[7]
        assert calls == ["drops"]

    def test_all_does_not_include_the_feed(self):
        expanded = sect.parse_include("all")
        assert "drops" not in expanded and "drop_ids" not in expanded
        assert "clog_slots" in expanded

    def test_the_feed_is_priced_per_day_of_window(self):
        from datetime import datetime, timedelta

        base = sect.REGISTRY["drops"].cost
        day = {"drops_window": (datetime(2026, 9, 1), datetime(2026, 9, 2))}
        week = {"drops_window": (datetime(2026, 8, 26), datetime(2026, 9, 2))}
        day_and_a_bit = {"drops_window": (datetime(2026, 9, 1), datetime(2026, 9, 2, 1))}
        assert sect.cost_of(["drops"], 1, day) == base
        assert sect.cost_of(["drops"], 1, week) == base * 7
        assert sect.cost_of(["drops"], 1, day_and_a_bit) == base * 2, "rounded up"
        assert sect.cost_of(["drops"], 1) == base, "no ctx: base weight"
        assert sect.cost_of(["drops", "drop_ids"], 1, day) == base, "the modifier is free"

    def test_the_feed_is_not_free_and_not_the_most_expensive_thing(self):
        # A day of drops for a page is cheaper than the same page's clog_slots
        # (measured ~2 ms vs ~8 ms per player), and it is not zero.
        assert 0 < sect.REGISTRY["drops"].cost < sect.REGISTRY["clog_slots"].cost


class TestUnixCoercion:
    def test_naive_datetime_is_read_as_utc(self):
        from datetime import datetime

        assert sect._unix(datetime(2026, 9, 2, 10, 0, 0)) == 1788343200

    def test_none_and_strings_are_none(self):
        assert sect._unix(None) is None
        assert sect._unix("0000-00-00 00:00:00") is None
