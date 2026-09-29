"""Unit tests for the prize-pot payout maths (services/event_payouts.py).

The module's top half is stdlib-only, so it is loaded by file path (the
test_event_prizes idiom) and exercised without the app or a database.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from fractions import Fraction

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_payouts.py",
)
_spec = importlib.util.spec_from_file_location("_event_payouts_under_test", _MODULE_PATH)
ep = importlib.util.module_from_spec(_spec)
sys.modules["_event_payouts_under_test"] = ep
_spec.loader.exec_module(ep)


class TestPlaceShares:
    def test_first_only(self):
        assert ep.place_shares("first_only", 3, [50, 50]) == [Fraction(1)]

    def test_top_n_is_even(self):
        assert ep.place_shares("top_n", 3, None) == [Fraction(1, 3)] * 3

    def test_custom_split(self):
        assert ep.place_shares("custom_split", 1, [60, 30, 10]) == [
            Fraction(60, 100), Fraction(30, 100), Fraction(10, 100)]

    def test_invalid_custom_split_falls_back_to_winner_takes_all(self):
        assert ep.place_shares("custom_split", 1, [60, 30]) == [Fraction(1)]


class TestAllocate:
    def test_simple_split_sums_to_total(self):
        shares = ep.place_shares("custom_split", 1, [60, 30, 10])
        out = ep.allocate(1000, shares, [("a", 1), ("b", 2), ("c", 3), ("d", 4)])
        assert {k: v["amount"] for k, v in out["entities"].items()} == {
            "a": 600, "b": 300, "c": 100, "d": 0}
        assert out["entities"]["d"]["share"] == 0
        assert out["unclaimed"] == 0 and out["rounding"] == 0

    def test_tie_pools_the_places_it_occupies(self):
        # Two teams tied for 1st on 60/30/10 take (60+30)/2 each; 3rd keeps 10.
        shares = ep.place_shares("custom_split", 1, [60, 30, 10])
        out = ep.allocate(1000, shares, [("a", 1), ("b", 1), ("c", 3)])
        ents = out["entities"]
        assert ents["a"]["amount"] == ents["b"]["amount"] == 450
        assert ents["a"]["tied"] and not ents["c"]["tied"]
        assert ents["c"]["amount"] == 100

    def test_tie_on_winner_takes_all_splits_the_pot(self):
        out = ep.allocate(1001, [Fraction(1)], [("a", 1), ("b", 1)])
        assert out["entities"]["a"]["amount"] == 500
        assert out["rounding"] == 1

    def test_unreached_places_are_unclaimed(self):
        shares = ep.place_shares("top_n", 3, None)
        out = ep.allocate(900, shares, [("a", 1)])
        assert out["entities"]["a"]["amount"] == 300
        assert out["unclaimed"] == 600
        assert out["unclaimed_places"] == [2, 3]

    def test_nobody_placed(self):
        out = ep.allocate(500, [Fraction(1)], [])
        assert out["entities"] == {}
        assert out["unclaimed"] == 500 and out["unclaimed_places"] == [1]

    def test_parts_always_sum_to_total(self):
        shares = ep.place_shares("top_n", 3, None)
        for total in (0, 1, 7, 1000, 123_456_789):
            out = ep.allocate(total, shares, [("a", 1), ("b", 2), ("c", 2)])
            paid = sum(v["amount"] for v in out["entities"].values())
            assert paid + out["unclaimed"] + out["rounding"] == total


class TestSplitEvenly:
    def test_even_with_remainder(self):
        split, rem = ep.split_evenly(100, [1, 2, 3])
        assert split == {1: 33, 2: 33, 3: 33} and rem == 1

    def test_no_members_keeps_the_whole_amount(self):
        assert ep.split_evenly(50, []) == ({}, 50)


def test_share_percent_rounds_for_display():
    assert ep.share_percent(Fraction(1, 3)) == 33.33
