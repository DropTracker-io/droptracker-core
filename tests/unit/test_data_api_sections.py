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

    def test_all_expands_to_every_section(self):
        assert set(sect.parse_include("all")) == set(sect.ALL_SECTION_KEYS)

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
        sweep = sect.cost_of(sect.ALL_SECTION_KEYS, 100) * 4
        assert sweep <= 200_000 * 0.75, sweep

    def test_the_two_heavy_sections_dominate_the_price(self):
        # Measured: clog_slots and loot_items are ~475x and ~357x the cheapest
        # section. If a change ever makes them comparable to the rest, the
        # weights have drifted from the measurement and need re-taking.
        heavy = sect.REGISTRY["clog_slots"].cost + sect.REGISTRY["loot_items"].cost
        everything = sum(sect.REGISTRY[k].cost for k in sect.ALL_SECTION_KEYS)
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

    def test_no_loader_reads_the_drops_table(self):
        # drops is 207M rows with exactly one safe access shape, which does not
        # survive a bulk page. The rollups exist so this stays true.
        # Matches the table in FROM/JOIN position only — 'SUM(drop_count) AS
        # drops' is an alias, not a read of the table.
        import re

        source = (_ROOT / "data_api" / "sections.py").read_text()
        body = source.split("# ── loaders", 1)[1]
        assert not re.search(r"\b(?:FROM|JOIN)\s+drops\b", body, re.IGNORECASE)

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
