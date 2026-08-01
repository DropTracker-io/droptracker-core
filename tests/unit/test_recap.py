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
           top_items=None, achievements=None, biggest=None, by_hour=None,
           ehb=None):
    """A minimal stored monthly payload, shaped like `_base_month_payload`."""
    totals = {"loot": loot, "drops": drops, "loot_rollup": loot}
    if ehb is not None:
        totals["ehb"] = ehb
    return {
        "period": period,
        "subject": {"id": 14, "name": "Test Clan"},
        "totals": totals,
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

    def test_ehb_folds_as_a_float(self, monkeypatch):
        # `totals` is summed through int(), which would round every month's
        # hours down. EHB is accumulated outside that loop for exactly this.
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 100, 10, ehb=12.4),
            "2025-02": _month("2025-02", 250, 25, ehb=8.3),
        })
        assert out["totals"]["ehb"] == 20.7

    def test_ehb_is_absent_when_no_month_carried_it(self, monkeypatch):
        # Every card written before web83a. A reader that finds no key omits
        # the stat; it must never read as "this clan bossed zero hours".
        out = self._fold(monkeypatch, {"2025-01": _month("2025-01", 100, 10)})
        assert "ehb" not in out["totals"]

    def test_ehb_sums_only_the_months_that_have_it(self, monkeypatch):
        out = self._fold(monkeypatch, {
            "2025-01": _month("2025-01", 100, 10),
            "2025-02": _month("2025-02", 250, 25, ehb=8.5),
        })
        assert out["totals"]["ehb"] == 8.5

    def test_annual_fold_states_no_previous_ehb(self, monkeypatch):
        # There is no previous *year* to compare against, so the card must not
        # be handed a baseline it would then draw a movement chip from.
        out = self._fold(monkeypatch, {"2025-01": _month("2025-01", 1, 1, ehb=3.0)})
        assert "previous_ehb" not in out["totals"]

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


def _item(item_id, name, loot, *, receiver=None, receivers=None, quantity=1):
    """A stored top_items entry, with optional attribution."""
    entry = {"item_id": item_id, "name": name, "loot": loot,
             "drops": 1, "quantity": quantity}
    if receiver is not None:
        entry["receiver"] = {"player_id": receiver[0], "name": receiver[1]}
    if receivers is not None:
        entry["receivers"] = receivers
    return entry


class TestReceiverFold:
    """Per-item attribution through the annual fold.

    A monthly snapshot names the top receiver *for that month*. Those can't be
    summed: if one clanmate got the June drop and another got the September one,
    printing either name over the year's total puts one person's name on
    another's drop. So attribution survives only while every folded month agrees
    on the same sole receiver, and the fold errs toward saying nothing.
    """

    def _fold(self, monkeypatch, months_by_period):
        monkeypatch.setattr(
            recap, "load_snapshot",
            lambda session, scope, subject_id, period: months_by_period.get(period),
        )
        return recap.compute_year(None, "group", 14, 2025)

    def _one_item(self, monkeypatch, months_by_period):
        out = self._fold(monkeypatch, months_by_period)
        assert len(out["top_items"]) == 1
        return out["top_items"][0]

    def test_sole_receiver_across_every_month_survives(self):
        # A genuine once-a-year drop: one month saw it, one person got it.
        agg = {"receivers": 0}
        recap._fold_receiver(agg, _item(1, "3rd age bow", 5, receiver=(9, "Redquaker"), receivers=1))
        assert agg["receiver"]["name"] == "Redquaker"
        assert agg["receivers"] == 1

    def test_same_sole_receiver_twice_still_survives(self):
        agg = {"receivers": 0}
        for _ in range(2):
            recap._fold_receiver(agg, _item(1, "x", 5, receiver=(9, "Chapsz"), receivers=1))
        assert agg["receiver"]["name"] == "Chapsz"
        assert agg["receivers"] == 1

    def test_two_months_disagreeing_drops_the_name(self):
        agg = {"receivers": 0}
        recap._fold_receiver(agg, _item(1, "x", 5, receiver=(9, "Chapsz"), receivers=1))
        recap._fold_receiver(agg, _item(1, "x", 5, receiver=(7, "Binny"), receivers=1))
        assert "receiver" not in agg
        # Distinct people across months = shared over the year, however sole it
        # was in either month.
        assert agg["receivers"] == 2

    def test_a_shared_month_drops_the_name(self):
        agg = {"receivers": 0}
        recap._fold_receiver(agg, _item(1, "x", 5, receiver=(9, "Chapsz"), receivers=4))
        assert "receiver" not in agg
        assert agg["receivers"] == 4

    def test_a_later_agreeing_month_cannot_resurrect_attribution(self):
        # Once two months have disagreed the item IS shared; a third month that
        # happens to match the first must not undo that.
        agg = {"receivers": 0}
        recap._fold_receiver(agg, _item(1, "x", 5, receiver=(9, "Chapsz"), receivers=1))
        recap._fold_receiver(agg, _item(1, "x", 5, receiver=(7, "Binny"), receivers=1))
        recap._fold_receiver(agg, _item(1, "x", 5, receiver=(9, "Chapsz"), receivers=1))
        assert "receiver" not in agg

    def test_missing_attribution_is_treated_as_shared(self):
        # An entry written before attribution existed, or one the source couldn't
        # name — must not be reported as sole.
        agg = {"receivers": 0}
        recap._fold_receiver(agg, _item(1, "x", 5))
        assert "receiver" not in agg
        assert agg["receivers"] >= 2

    def test_end_to_end_sole_receiver_reaches_the_payload(self, monkeypatch):
        item = self._one_item(monkeypatch, {
            "2025-03": _month("2025-03", 1, 1,
                              top_items=[_item(1, "3rd age bow", 100,
                                               receiver=(9, "Redquaker"), receivers=1)]),
        })
        assert item["receiver"]["name"] == "Redquaker"
        assert item["loot"] == 100

    def test_end_to_end_conflicting_receivers_reach_the_payload_unnamed(self, monkeypatch):
        item = self._one_item(monkeypatch, {
            "2025-03": _month("2025-03", 1, 1,
                              top_items=[_item(1, "Twisted bow", 100,
                                               receiver=(9, "A"), receivers=1)]),
            "2025-07": _month("2025-07", 1, 1,
                              top_items=[_item(1, "Twisted bow", 50,
                                               receiver=(7, "B"), receivers=1)]),
        })
        assert "receiver" not in item
        assert item["loot"] == 150  # value still folds; only the name is dropped

    def test_fold_bookkeeping_never_leaks_into_the_payload(self, monkeypatch):
        # `_recv_conflict` is internal state; a stored payload carrying it would
        # ship to the client and outlive the fold that created it.
        out = self._fold(monkeypatch, {
            "2025-03": _month("2025-03", 1, 1,
                              top_items=[_item(1, "x", 10, receiver=(9, "A"), receivers=1)],
                              top_npcs=[{"npc_id": 5, "name": "ToB", "loot": 1, "drops": 1}]),
            "2025-04": _month("2025-04", 1, 1,
                              top_items=[_item(1, "x", 10, receiver=(7, "B"), receivers=1)]),
        })
        for entry in (*out["top_items"], *out["top_npcs"]):
            assert not [k for k in entry if k.startswith("_")], entry


class TestPlayerMonth:
    """`compute_player_month` — the personal card.

    Its DB and Redis reads are stubbed; what's under test is the set of
    judgements it makes on top of them: who is allowed a card at all, and what
    the card is permitted to claim.
    """

    def _compute(self, monkeypatch, *, drops=100, hidden=False, rank=(34, 4812),
                 prev_rank=(74, 4500), groups=None, top_items=None,
                 ehb=None, prev_ehb=None):
        ranks = {202506: rank, 202505: prev_rank}
        gains = {"2025-06": ehb, "2025-05": prev_ehb}
        monkeypatch.setattr(
            recap, "_ehb_gains",
            lambda session, pids, period: (
                {795: gains[period]} if gains.get(period) is not None else {}
            ),
        )
        monkeypatch.setattr(recap, "_player_is_hidden", lambda pid: hidden)
        monkeypatch.setattr(
            recap, "_base_month_payload",
            lambda session, pids, period: {
                "totals": {"drops": drops, "loot_rollup": 999, "unique_items": 7},
                "top_items": [dict(i) for i in (top_items or [])],
                "top_npcs": [],
                "achievements": {},
            },
        )
        monkeypatch.setattr(recap, "_redis_totals",
                            lambda pids, partition: {795: 1_000 if partition == 202506 else 500})
        monkeypatch.setattr(recap, "_player_rank",
                            lambda pid, partition: ranks.get(partition, (None, None)))
        monkeypatch.setattr(recap, "_player_names", lambda session, pids: {795: "Buzzyn"})
        monkeypatch.setattr(recap, "_player_groups",
                            lambda session, pid, **kw: groups if groups is not None else [])
        monkeypatch.setattr(recap, "_finalize", lambda payload, period: payload)
        return recap.compute_player_month(None, 795, "2025-06")

    def test_hidden_player_gets_no_card(self, monkeypatch):
        # A personal card is a permanent public URL about one person; opting out
        # of public display has to mean this too.
        assert self._compute(monkeypatch, hidden=True) is None

    def test_below_the_activity_floor_gets_no_card(self, monkeypatch):
        assert self._compute(monkeypatch, drops=recap.MIN_DROPS_FOR_RECAP - 1) is None

    def test_at_the_activity_floor_gets_a_card(self, monkeypatch):
        # The floor is inclusive — a player with exactly the minimum qualifies.
        assert self._compute(monkeypatch, drops=recap.MIN_DROPS_FOR_RECAP) is not None

    def test_headline_loot_comes_from_redis_not_the_rollup(self, monkeypatch):
        out = self._compute(monkeypatch)
        assert out["totals"]["loot"] == 1_000
        assert out["totals"]["loot_rollup"] == 999

    def test_percentile_is_position_over_board_size(self, monkeypatch):
        out = self._compute(monkeypatch, rank=(34, 4812))
        assert out["rank"]["percentile"] == round(100.0 * 34 / 4812, 1)

    def test_percentile_is_none_without_a_rank(self, monkeypatch):
        out = self._compute(monkeypatch, rank=(None, None))
        assert out["rank"]["percentile"] is None

    def test_previous_placing_is_read_from_the_previous_month(self, monkeypatch):
        out = self._compute(monkeypatch, rank=(34, 4812), prev_rank=(74, 4500))
        assert out["rank"]["previous_position"] == 74
        assert out["rank"]["previous_of"] == 4500

    def test_previous_placing_is_stated_not_differenced(self, monkeypatch):
        # The board grows, so a position delta can report a player who held
        # still as having fallen. Both numbers ship; no delta does.
        out = self._compute(monkeypatch)
        assert "delta" not in out["rank"]
        assert "movement" not in out["rank"]
        assert "places" not in out["rank"]

    def test_previous_placing_absent_when_they_were_unranked(self, monkeypatch):
        out = self._compute(monkeypatch, prev_rank=(None, None))
        assert out["rank"]["previous_position"] is None

    def test_clans_land_on_the_subject(self, monkeypatch):
        out = self._compute(monkeypatch, groups=[{"id": 14, "name": "Pegasus PvM"}])
        assert out["subject"] == {
            "id": 795, "name": "Buzzyn",
            "groups": [{"id": 14, "name": "Pegasus PvM"}],
        }

    def test_clanless_player_gets_an_empty_group_list(self, monkeypatch):
        out = self._compute(monkeypatch, groups=[])
        assert out["subject"]["groups"] == []

    def test_item_attribution_is_stripped(self, monkeypatch):
        # Naming the receiver on a player's own card is tautological — every item
        # in it was theirs.
        out = self._compute(monkeypatch, top_items=[
            _item(1, "Imbued heart", 100, receiver=(795, "Buzzyn"), receivers=1),
        ])
        assert "receiver" not in out["top_items"][0]
        assert "receivers" not in out["top_items"][0]
        assert out["top_items"][0]["name"] == "Imbued heart"

    def test_scope_is_player(self, monkeypatch):
        assert self._compute(monkeypatch)["scope"] == recap.SCOPE_PLAYER

    def test_ehb_is_reported_with_its_previous_month(self, monkeypatch):
        out = self._compute(monkeypatch, ehb=41.27, prev_ehb=33.5)
        assert out["totals"]["ehb"] == 41.3
        assert out["totals"]["previous_ehb"] == 33.5

    def test_unharvested_player_has_no_ehb_at_all(self, monkeypatch):
        # WOM hasn't answered for them. That is *unknown*, and a card that
        # printed "0 EHB" would be claiming something nobody measured.
        out = self._compute(monkeypatch, ehb=None, prev_ehb=None)
        assert "ehb" not in out["totals"]
        assert "previous_ehb" not in out["totals"]

    def test_a_first_harvested_month_has_no_baseline(self, monkeypatch):
        # The month before was never harvested, so there is nothing to move
        # from — the figure ships, the comparison doesn't.
        out = self._compute(monkeypatch, ehb=41.3, prev_ehb=None)
        assert out["totals"]["ehb"] == 41.3
        assert "previous_ehb" not in out["totals"]

    def test_a_measured_zero_is_still_a_number(self, monkeypatch):
        # Harvested and genuinely idle is not the same as unharvested: the
        # stored row says so, and next month's comparison depends on it.
        out = self._compute(monkeypatch, ehb=0.0, prev_ehb=12.0)
        assert out["totals"]["ehb"] == 0.0


class TestPublicImageUrl:
    """`drops.image_url` is not reliably a URL — some rows hold the on-disk path
    the screenshot was written to. Rendering that draws a broken-image box on the
    poster, so the card would rather show nothing."""

    def test_absolute_urls_pass_through(self):
        url = "https://www.droptracker.io/img/user-upload/2682265/drop/x/y_0_2.jpg"
        assert recap._public_image_url(url) == url

    def test_local_path_is_mapped_to_the_served_url(self):
        assert recap._public_image_url(
            "/store/droptracker/disc/static/assets/img/user-upload/1956709/drop/unknown/u.png"
        ) == "https://www.droptracker.io/img/user-upload/1956709/drop/unknown/u.png"

    def test_site_relative_path_is_made_absolute(self):
        # A snapshot is an archive that may be rendered off-origin.
        assert recap._public_image_url("/img/user-upload/1/drop/a.png") == (
            "https://www.droptracker.io/img/user-upload/1/drop/a.png"
        )

    def test_unservable_values_are_dropped(self):
        for raw in (None, "", "   ", "file:///tmp/x.png", "somewhere/else.png"):
            assert recap._public_image_url(raw) is None

    def test_no_double_slash_when_mapping(self):
        out = recap._public_image_url(
            "/store/droptracker/disc/static/assets/img/user-upload/9/drop/a.png"
        )
        assert "//img" not in out.replace("https://", "")
        assert out.count("//") == 1  # only the scheme's
