"""Parsers and routing in scripts/sync_wiki_drops.py.

The wiki's Bucket data differs from the old SMW payloads in exactly the ways
these tests pin down: comma/decimal rarity fractions, en-dash quantity
ranges, the vanished ``Noted`` key, and mode-anchored ``Dropped from``
values on reward-chest pages.
"""

import pytest

from scripts.sync_wiki_drops import (
    ANCHOR_ROUTES,
    _db_float,
    desired_rows_for_page,
    parse_quantity,
    parse_rarity,
)


class TestParseRarity:
    @pytest.mark.parametrize("raw,expected", [
        ("Always", 1.0),
        ("always", 1.0),
        ("30/128", 30 / 128),
        ("1/128", 1 / 128),
        # The Bucket rendering that broke the legacy PHP regex: thousands
        # separators and decimals ("1/2,500.92" must not parse as 1/2).
        ("1/2,500.92", 1 / 2500.92),
        ("1/2,501", 1 / 2501),
        ("3/1,000", 3 / 1000),
        (0.5, 0.5),
        (1, 1.0),
        ("0.25", 0.25),
    ])
    def test_parses(self, raw, expected):
        assert parse_rarity(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [
        None, "", "Varies", "Common", "Rare", "Unknown", "Once", "1/0",
    ])
    def test_unparseable_is_zero(self, raw):
        assert parse_rarity(raw) == 0.0


class TestParseQuantity:
    def test_explicit_quantity_wins(self):
        assert parse_quantity({"Drop Quantity": "4", "Quantity Low": 1,
                               "Quantity High": 9}) == "4"

    def test_en_dash_normalized(self):
        assert parse_quantity({"Drop Quantity": "1–16"}) == "1-16"

    def test_low_high_fallback(self):
        assert parse_quantity({"Quantity Low": 100, "Quantity High": 150}) == "100-150"

    def test_defaults_to_one(self):
        assert parse_quantity({}) == "1"

    def test_bounded_to_column_width(self):
        assert len(parse_quantity({"Drop Quantity": "x" * 99})) <= 50


def _bucket_row(item, page="Some Boss", dropped_from=None, quantity="1",
                rarity="1/128", notes=""):
    import json
    return {
        "page_name": page,
        "item_name": item,
        "drop_json": json.dumps({
            "Dropped item": item,
            "Dropped from": dropped_from or page,
            "Drop Quantity": quantity,
            "Rarity": rarity,
            "Rolls": 1,
            "Name Notes": notes,
        }),
    }


ITEM_IDS = {"Coins": 995, "Uncut diamond": 1617, "Crystal shard": 23962}


class TestDesiredRows:
    def test_plain_page_maps_to_page_name(self):
        rows, unresolved = desired_rows_for_page(
            "Some Boss", [_bucket_row("Uncut diamond", rarity="1/2,500.92")],
            ITEM_IDS)
        assert not unresolved
        assert set(rows) == {"Some Boss"}
        ((iid, qty, noted, rarity, rolls),) = rows["Some Boss"]
        assert (iid, qty, noted, rolls) == (1617, "1", 0, 1)
        assert rarity == pytest.approx(_db_float(1 / 2500.92))

    def test_unresolved_items_reported_not_dropped(self):
        rows, unresolved = desired_rows_for_page(
            "Some Boss", [_bucket_row("No Such Item")], ITEM_IDS)
        assert unresolved == {"No Such Item"}
        assert rows == {}

    def test_item_anchor_falls_back_to_base_name(self):
        rows, unresolved = desired_rows_for_page(
            "Some Boss", [_bucket_row("Uncut diamond#Shiny")], ITEM_IDS)
        assert not unresolved
        assert len(rows["Some Boss"]) == 1

    def test_noted_from_quantity_annotation(self):
        rows, _ = desired_rows_for_page(
            "Some Boss", [_bucket_row("Coins", quantity="100 (noted)")],
            ITEM_IDS)
        ((_, _, noted, _, _),) = rows["Some Boss"]
        assert noted == 1

    def test_gauntlet_anchors_split_modes(self):
        page = "Reward Chest (The Gauntlet)"
        rows, _ = desired_rows_for_page(page, [
            _bucket_row("Uncut diamond", page=page, dropped_from=f"{page}#Corrupted"),
            _bucket_row("Coins", page=page, dropped_from=f"{page}#Regular"),
            _bucket_row("Crystal shard", page=page, dropped_from=page),
        ], ITEM_IDS)
        corrupted = {iid for iid, *_ in rows["The Corrupted Gauntlet"]}
        regular = {iid for iid, *_ in rows["The Gauntlet"]}
        assert corrupted == {1617, 23962}   # corrupted line + shared line
        assert regular == {995, 23962}      # regular line + shared line

    def test_tob_shared_lines_reach_both_modes(self):
        page = "Monumental chest"
        rows, _ = desired_rows_for_page(page, [
            _bucket_row("Coins", page=page, dropped_from=f"{page}#Hard Mode"),
            _bucket_row("Uncut diamond", page=page, dropped_from=page),
        ], ITEM_IDS)
        assert {iid for iid, *_ in rows["Theatre of Blood"]} == {1617}
        assert {iid for iid, *_ in rows["Theatre of Blood: Hard Mode"]} == {995, 1617}

    def test_anchor_route_pages_all_have_none_fallback(self):
        for page, route in ANCHOR_ROUTES.items():
            assert None in route, page
