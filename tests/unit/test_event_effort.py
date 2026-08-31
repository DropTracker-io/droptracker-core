"""Unit tests for Bingo EHB — event-scoped effort (services/event_effort.py
plus the engine's relevance/freeze wiring).

The rules worth pinning down are the ones the suggestion thread argued about:
which bosses count (task-named, plus inferred item sources — "Yama KC counts
toward the Oathplate tile"), what an unpriceable boss is worth (activity, but
0 EHB — never a guess), and that effort stops the moment the tile it feeds is
done.

The engine is loaded by file path like test_event_kc_watermark.py so the
conftest sys.modules stubs never interfere.
"""

import importlib.util
import os
import sys
from datetime import datetime

import pytest

from services.event_effort import (
    CLUE_TIERS,
    EFFORT_SCOPE_PREFIX,
    build_effort_map,
    clue_roll_npc,
    clue_tier,
    completion_marker,
    completion_scope,
    effort_scope,
    ehb_hours,
    is_completion_drop,
    roll_scope,
    rows_to_summary,
)

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_effort_test", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_effort_test"] = engine
_spec.loader.exec_module(engine)


# WOM's real published rates for these bosses (kills/hour).
RATES = {"yama": 18.0, "zulrah": 46.0, "vorkath": 34.0}


def _metric(name):
    """Stand-in for utils.wiseoldman.wom_boss_metric: slugify, and return None
    for anything WOM doesn't track (chest/activity sources)."""
    slug = name.replace(" ", "_")
    return slug if slug in {"yama", "zulrah", "vorkath", "lunar_chest"} else None


class TestRelevance:
    """Which bosses an event's tasks make relevant."""

    def test_kc_target_npcs_are_relevant(self):
        out = build_effort_map(
            [{"task_id": 1, "npcs": ["zulrah"], "npc_ids": {"zulrah": 20}}],
            resolve_sources=lambda names: {},
            boss_metric=_metric,
        )
        assert out["zulrah"]["npc_id"] == 20
        assert out["zulrah"]["metric"] == "zulrah"
        assert out["zulrah"]["tasks"] == [1]

    def test_item_task_infers_its_drop_sources(self):
        # The OP's example: an Oathplate tile names no NPC, but Yama drops it.
        out = build_effort_map(
            [{"task_id": 7, "npcs": [], "item_names": ["Oathplate helm"]}],
            resolve_sources=lambda names: {"Yama": 14176},
            boss_metric=_metric,
        )
        assert out["yama"] == {"npc_id": 14176, "metric": "yama", "tasks": [7]}

    def test_configured_sources_win_over_inference(self):
        # An admin who already restricted sources has answered the question
        # inference exists to guess at — the descriptor carries no item names.
        called = []

        def resolve(names):
            called.append(names)
            return {"Zulrah": 20}

        out = build_effort_map(
            [{"task_id": 7, "npcs": ["yama"], "npc_ids": {"yama": 14176},
              "item_names": []}],
            resolve_sources=resolve,
            boss_metric=_metric,
        )
        assert set(out) == {"yama"}
        assert called == []

    def test_one_npc_serving_several_tasks_is_one_entry(self):
        out = build_effort_map(
            [{"task_id": 1, "npcs": ["yama"], "npc_ids": {"yama": 14176}},
             {"task_id": 2, "npcs": ["yama"], "npc_ids": {"yama": 14176}}],
            resolve_sources=lambda names: {},
            boss_metric=_metric,
        )
        # One in-game kill counter, so one row — but it knows both tasks, which
        # is what stops it freezing while either is still open.
        assert list(out) == ["yama"]
        assert out["yama"]["tasks"] == [1, 2]

    def test_boss_without_a_wom_metric_is_still_tracked(self):
        # Real activity we can't price. Dropping it would hide the player from
        # the inactivity report, which is worse than showing 0 EHB.
        out = build_effort_map(
            [{"task_id": 1, "npcs": ["barrows"], "npc_ids": {"barrows": 3}}],
            resolve_sources=lambda names: {},
            boss_metric=_metric,
        )
        assert out["barrows"]["metric"] is None

    def test_inference_failure_degrades_instead_of_raising(self):
        def boom(names):
            raise RuntimeError("wiki down")

        out = build_effort_map(
            [{"task_id": 1, "npcs": ["yama"], "npc_ids": {"yama": 14176}},
             {"task_id": 2, "npcs": [], "item_names": ["Twisted bow"]}],
            resolve_sources=boom,
            boss_metric=_metric,
        )
        assert set(out) == {"yama"}

    def test_cap_drops_guesses_before_explicit_npcs(self):
        out = build_effort_map(
            [{"task_id": 1, "npcs": ["yama"], "npc_ids": {"yama": 14176}},
             {"task_id": 2, "npcs": [], "item_names": ["Uncut ruby"]}],
            resolve_sources=lambda names: {f"Boss {i}": i for i in range(50)},
            boss_metric=_metric,
            max_npcs=3,
        )
        # The named boss survives; only inferred sources are sacrificed.
        assert "yama" in out
        assert len(out) == 3


class TestEhbMath:
    def test_kills_are_priced_at_the_wom_rate(self):
        assert ehb_hours({"zulrah": 46}, RATES) == pytest.approx(1.0)
        assert ehb_hours({"yama": 18}, RATES) == pytest.approx(1.0)

    def test_several_bosses_sum(self):
        assert ehb_hours({"zulrah": 46, "vorkath": 34}, RATES) == pytest.approx(2.0)

    def test_metric_without_a_rate_contributes_nothing(self):
        # Honest zero: we do not invent a rate for content WOM hasn't priced.
        assert ehb_hours({"lunar_chest": 500}, RATES) == 0.0

    def test_empty_rate_table_is_zero_not_a_crash(self):
        # Cold cache (nothing has fetched WOM's rates yet).
        assert ehb_hours({"zulrah": 46}, {}) == 0.0
        assert ehb_hours({}, RATES) == 0.0

    def test_junk_values_are_skipped(self):
        assert ehb_hours({"zulrah": None, "vorkath": "34"}, RATES) == pytest.approx(1.0)


class TestSummary:
    def _rows(self):
        return [
            {"npc_id": 1, "npc_name": "Zulrah", "boss_metric": "zulrah",
             "kills": 46, "last_at": 100, "frozen_at": None},
            {"npc_id": 2, "npc_name": "Yama", "boss_metric": "yama",
             "kills": 90, "last_at": 200, "frozen_at": "sometime"},
        ]

    def test_totals_and_ordering(self):
        out = rows_to_summary(self._rows(), RATES)
        assert out["kills"] == 136
        assert out["ehb_hours"] == pytest.approx(6.0)  # 1h Zulrah + 5h Yama
        # Biggest investment first, not alphabetical or insertion order.
        assert [b["name"] for b in out["bosses"]] == ["Yama", "Zulrah"]
        assert out["last_at"] == 200
        assert out["frozen"] == 1

    def test_zero_kill_rows_are_dropped(self):
        out = rows_to_summary(
            [{"npc_id": 1, "npc_name": "Zulrah", "boss_metric": "zulrah",
              "kills": 0, "last_at": 1, "frozen_at": None}], RATES)
        assert out["bosses"] == [] and out["kills"] == 0

    def test_unpriced_boss_still_counts_kills(self):
        out = rows_to_summary(
            [{"npc_id": 3, "npc_name": "Lunar Chest", "boss_metric": "lunar_chest",
              "kills": 300, "last_at": 5, "frozen_at": None}], RATES)
        assert out["kills"] == 300
        assert out["ehb_hours"] == 0.0
        assert out["bosses"][0]["kills"] == 300


class TestDerivedRates:
    """Fallback pricing from npc_ehb_rates — our own estimates for bosses WOM
    doesn't publish a rate for (thread #93: approximation is fine, labelled,
    and a derived rate must never silently replace a WOM one)."""

    def _row(self, **over):
        row = {"npc_id": 15742, "npc_name": "The Maggot King",
               "boss_metric": "maggot_king", "kills": 130,
               "last_at": 1, "frozen_at": None}
        row.update(over)
        return row

    def test_unpriced_metric_falls_back_to_the_derived_rate(self):
        # The motivating case: 130 Maggot King kills read "0h" before this.
        out = rows_to_summary([self._row()], RATES, {15742: 26.0})
        assert out["ehb_hours"] == pytest.approx(5.0)
        assert out["ehb_estimated_hours"] == pytest.approx(5.0)
        assert out["bosses"][0]["estimated"] is True

    def test_wom_rate_wins_even_when_a_derived_rate_exists(self):
        out = rows_to_summary(
            [{"npc_id": 1, "npc_name": "Zulrah", "boss_metric": "zulrah",
              "kills": 46, "last_at": 1, "frozen_at": None}],
            RATES, {1: 10.0})
        # 46 kills at WOM's 46/h, not at the derived 10/h.
        assert out["ehb_hours"] == pytest.approx(1.0)
        assert out["ehb_estimated_hours"] == 0.0
        assert out["bosses"][0]["estimated"] is False

    def test_metricless_npc_is_priced_by_npc_id(self):
        # A source WOM doesn't track at all can still carry a derived rate.
        out = rows_to_summary(
            [self._row(boss_metric=None, kills=52)], RATES, {15742: 52.0})
        assert out["ehb_hours"] == pytest.approx(1.0)
        assert out["bosses"][0]["estimated"] is True

    def test_no_derived_entry_keeps_the_honest_zero(self):
        out = rows_to_summary([self._row()], RATES, {9999: 26.0})
        assert out["ehb_hours"] == 0.0
        assert out["ehb_estimated_hours"] == 0.0
        assert out["bosses"][0]["estimated"] is False
        assert out["kills"] == 130  # activity still counts

    def test_none_derived_map_is_the_old_behaviour(self):
        out = rows_to_summary([self._row()], RATES, None)
        assert out["ehb_hours"] == 0.0
        assert out["bosses"][0]["estimated"] is False

    def test_junk_derived_values_are_skipped(self):
        for junk in ({15742: 0}, {15742: -5}, {15742: None}, {15742: "x"}):
            out = rows_to_summary([self._row()], RATES, junk)
            assert out["ehb_hours"] == 0.0, junk

    def test_estimated_hours_are_a_subset_not_an_addition(self):
        rows = [
            {"npc_id": 1, "npc_name": "Zulrah", "boss_metric": "zulrah",
             "kills": 46, "last_at": 1, "frozen_at": None},
            self._row(kills=26),
        ]
        out = rows_to_summary(rows, RATES, {15742: 26.0})
        assert out["ehb_hours"] == pytest.approx(2.0)       # 1h WOM + 1h derived
        assert out["ehb_estimated_hours"] == pytest.approx(1.0)
        # Ordering still by contribution: equal hours fall back to kills.
        assert [b["name"] for b in out["bosses"]] == ["Zulrah", "The Maggot King"]


class TestCompletionMarkers:
    """Partial-credit NPCs — a bail must not be priced as a completion.

    The Colosseum pays out a reward chest whenever you leave, so the plugin's
    KC counts attempts while WOM's ``sol_heredit`` only counts the wave-12
    kill. Charging every attempt the completion rate put one player at 329 EHE
    hours on 2026-08-28 (2.7 completions/hour = 22 min each, against measured
    bails of about two minutes). Completions and partials are counted and
    priced separately; these pin that down.
    """

    # 2.7 completions/h is WOM's published sol_heredit rate; 50.0 partials/h
    # is the measured partial_gaps rate (72s a bail).
    RATES = {"sol_heredit": 2.7}
    DERIVED = {13741: 50.0}

    def _row(self, kills, completions, **over):
        row = {"npc_id": 13741, "npc_name": "Fortis Colosseum",
               "boss_metric": "sol_heredit", "kills": kills,
               "completions": completions, "last_at": 1, "frozen_at": None}
        row.update(over)
        return row

    def test_registry_recognises_the_marker_drop(self):
        assert completion_marker("Fortis Colosseum")["metric"] == "sol_heredit"
        assert completion_marker("fortis  colosseum") is not None  # normalized
        assert completion_marker("Zulrah") is None
        assert is_completion_drop("Fortis Colosseum",
                                  "Dizana's quiver (uncharged)") is True
        # Every other item from the same chest is not proof of a completion.
        assert is_completion_drop("Fortis Colosseum", "Sunfire splinters") is False
        assert is_completion_drop("Zulrah", "Dizana's quiver (uncharged)") is False

    def test_completions_and_partials_price_separately(self):
        # iZuny's real event-46 row: 16 attempts, 12 of them completions.
        out = rows_to_summary([self._row(16, 12)], self.RATES, self.DERIVED)
        assert out["ehb_hours"] == pytest.approx(12 / 2.7 + 4 / 50.0)
        assert out["kills"] == 16
        # Only the partial portion is our own estimate.
        assert out["ehb_estimated_hours"] == pytest.approx(4 / 50.0)
        assert out["bosses"][0]["estimated"] is True

    def test_the_splinter_farmer_is_no_longer_charged_74_hours(self):
        # 200 attempts, 5 completions — the shape the old model punished most.
        out = rows_to_summary([self._row(200, 5)], self.RATES, self.DERIVED)
        old = 200 / 2.7
        assert old == pytest.approx(74.07, abs=0.01)
        assert out["ehb_hours"] == pytest.approx(5 / 2.7 + 195 / 50.0)
        assert out["ehb_hours"] < 6.0

    def test_all_completions_still_price_at_the_full_wom_rate(self):
        # The split must not quietly shortchange someone who finishes every run.
        out = rows_to_summary([self._row(12, 12)], self.RATES, self.DERIVED)
        assert out["ehb_hours"] == pytest.approx(12 / 2.7)
        assert out["ehb_estimated_hours"] == 0.0
        assert out["bosses"][0]["estimated"] is False

    def test_wom_only_player_keeps_their_completions(self):
        """WOM reports completions for a player whose plugin never sent an
        attempt, so ``completions`` may exceed ``kills`` and the attempt total
        is max() of the two — otherwise the row is dropped as zero-kill."""
        out = rows_to_summary([self._row(0, 9)], self.RATES, self.DERIVED)
        assert out["kills"] == 9
        assert out["ehb_hours"] == pytest.approx(9 / 2.7)

    def test_a_missing_partial_rate_costs_the_partials_not_the_completions(self):
        out = rows_to_summary([self._row(16, 12)], self.RATES, {})
        # Partials contribute the honest zero; completions are unaffected.
        assert out["ehb_hours"] == pytest.approx(12 / 2.7)

    def test_a_cold_wom_cache_still_prices_the_partials(self):
        out = rows_to_summary([self._row(16, 12)], {}, self.DERIVED)
        assert out["ehb_hours"] == pytest.approx(4 / 50.0)

    def test_backfill_gap_reads_as_all_partials_not_all_completions(self):
        """A row written before web104a has completions=0. That must under-
        price rather than over-price — the whole point of the change."""
        out = rows_to_summary([self._row(16, 0)], self.RATES, self.DERIVED)
        assert out["ehb_hours"] == pytest.approx(16 / 50.0)
        assert out["ehb_hours"] < 16 / 2.7

    def test_junk_completions_do_not_break_pricing(self):
        for junk in (None, "", "x", -3):
            out = rows_to_summary([self._row(10, junk)], self.RATES, self.DERIVED)
            assert out["ehb_hours"] == pytest.approx(10 / 50.0), junk

    def test_ordinary_bosses_ignore_the_completions_column(self):
        out = rows_to_summary(
            [{"npc_id": 1, "npc_name": "Zulrah", "boss_metric": "zulrah",
              "kills": 46, "completions": 3, "last_at": 1, "frozen_at": None}],
            RATES, {1: 10.0})
        assert out["ehb_hours"] == pytest.approx(1.0)

    def test_completion_scope_is_distinct_from_the_attempt_scope(self):
        # Folding completions into the attempts watermark is precisely the
        # unit confusion behind report #131.
        assert completion_scope("Fortis Colosseum") == "effc:fortis_colosseum"
        assert completion_scope("Fortis Colosseum") != effort_scope(
            "Fortis Colosseum")


class TestCluePairing:
    """Suggestion #156 — clue tiers earn hours only for openings that were
    matched by a scroll the player was dealt inside the window.

    Caskets bank, so openings alone measure nothing: joel's objection to the
    whole idea was "stack 100 elites, spam open them, 100x the event length in
    EHE in the first 5 minutes". The pairing rule is Fazebook's answer to it.
    """

    def _row(self, npc="Clue Scroll (Hard)", npc_id=13945, kills=0, rolls=0):
        return {"npc_id": npc_id, "npc_name": npc, "boss_metric": None,
                "kills": kills, "rolls": rolls, "last_at": None,
                "frozen_at": None}

    def test_every_tier_has_a_rate(self):
        assert set(CLUE_TIERS) == {
            f"clue scroll ({t})" for t in
            ("beginner", "easy", "medium", "hard", "elite", "master")
        }
        assert all(e["rate_kph"] > 0 for e in CLUE_TIERS.values())
        # Harder tiers take longer; the registry must stay monotonic or a
        # master clue starts pricing cheaper than an elite.
        order = ["medium", "hard", "elite", "master"]
        rates = [CLUE_TIERS[f"clue scroll ({t})"]["rate_kph"] for t in order]
        assert rates == sorted(rates, reverse=True)

    def test_a_scroll_box_is_a_roll_for_its_tier(self):
        # The case that decides whether this feature works at all: monsters
        # drop boxes, not scrolls (3,703 hard boxes to 6 hard scrolls in a
        # week of prod drops).
        assert clue_roll_npc("Scroll box (hard)") == "clue scroll (hard)"
        assert clue_roll_npc("Clue scroll (hard)") == "clue scroll (hard)"
        assert clue_roll_npc("Clue bottle (medium)") == "clue scroll (medium)"
        assert clue_roll_npc("Clue nest (elite)") == "clue scroll (elite)"
        assert clue_roll_npc("Clue geode (easy)") == "clue scroll (easy)"

    def test_a_reward_casket_is_not_a_roll(self):
        """A casket is the clue's REWARD. Counting one as a roll would let a
        banked stack pay for itself as it is opened."""
        assert clue_roll_npc("Reward casket (hard)") is None

    def test_untiered_clue_items_are_not_rolls(self):
        assert clue_roll_npc("Clue scroll") is None
        assert clue_roll_npc("Clue scroll (special)") is None
        assert clue_roll_npc(None) is None

    def test_a_dumped_stack_prices_nothing(self):
        """joel's objection, priced: 100 banked elites opened, none rolled."""
        out = rows_to_summary(
            [self._row("Clue Scroll (Elite)", 13944, kills=100, rolls=0)], {})
        assert out["ehb_hours"] == 0
        # The openings are still real activity and still show.
        assert out["kills"] == 100
        assert out["bosses"][0]["paired"] == 0
        assert out["bosses"][0]["rolled"] == 0

    def test_fazebooks_example(self):
        """"If 20 clues are rolled and 25 clues opened, count the first 20"."""
        out = rows_to_summary([self._row(kills=25, rolls=20)], {})
        boss = out["bosses"][0]
        assert boss["paired"] == 20
        assert boss["kills"] == 25
        assert out["ehb_hours"] == pytest.approx(20 / 7.8)

    def test_rolls_beyond_what_was_opened_pay_nothing(self):
        """The mirror case: hoarding scrolls is not doing clues."""
        out = rows_to_summary([self._row(kills=5, rolls=40)], {})
        assert out["bosses"][0]["paired"] == 5
        assert out["ehb_hours"] == pytest.approx(5 / 7.8)

    def test_clue_hours_are_always_an_estimate(self):
        """WOM prices no clue metric, so every hour here is ours."""
        out = rows_to_summary([self._row(kills=10, rolls=10)], {})
        assert out["bosses"][0]["estimated"] is True
        assert out["ehb_estimated_hours"] == pytest.approx(out["ehb_hours"])

    def test_a_derived_rate_row_retunes_the_tier(self):
        """An npc_ehb_rates row lets the owner retune a tier without a deploy."""
        out = rows_to_summary([self._row(kills=10, rolls=10)], {}, {13945: 5.0})
        assert out["ehb_hours"] == pytest.approx(10 / 5.0)

    def test_a_wom_rate_cannot_price_a_clue_tier(self):
        """Belt and braces: clue pseudo-NPCs carry no boss metric, and a stray
        one must not route the row back onto the WOM path — that would price
        every unpaired opening."""
        row = self._row(kills=25, rolls=0)
        row["boss_metric"] = "hard_clue"
        out = rows_to_summary([row], {"hard_clue": 5.0})
        assert out["ehb_hours"] == 0

    def test_junk_roll_values_do_not_break_pricing(self):
        for junk in (None, "", "many", -4, object()):
            out = rows_to_summary([self._row(kills=10, rolls=junk)], {})
            assert out["ehb_hours"] == 0
            assert out["bosses"][0]["paired"] == 0

    def test_ordinary_bosses_carry_no_pair(self):
        out = rows_to_summary(
            [{"npc_id": 15742, "npc_name": "Yama", "boss_metric": "yama",
              "kills": 40, "last_at": None, "frozen_at": None}], RATES)
        assert out["bosses"][0]["rolled"] is None
        assert out["bosses"][0]["paired"] is None
        assert out["ehb_hours"] == pytest.approx(40 / 18.0)

    def test_roll_scope_is_distinct_from_the_other_two(self):
        npc = "clue scroll (hard)"
        assert len({roll_scope(npc), effort_scope(npc), completion_scope(npc)}) == 3
        assert roll_scope(npc) == "effr:clue_scroll_(hard)"

    def test_clue_tier_lookup_normalizes_like_the_engine(self):
        assert clue_tier("  Clue   Scroll (Hard) ") is CLUE_TIERS["clue scroll (hard)"]
        assert clue_tier("Yama") is None


class TestClueRollRouting:
    """Which counter a clue-bearing envelope feeds.

    A roll and an opening happen at DIFFERENT NPCs — the scroll comes off some
    boss's drop table, the casket is opened under the tier's pseudo-NPC — so
    the two must never consume each other's watermark, and a drop that is both
    a kill and a roll has to credit both.
    """

    NPCS = {
        "clue scroll (hard)": {"npc_id": 13945, "metric": None, "tasks": []},
        "clue scroll (elite)": {"npc_id": 13944, "metric": None, "tasks": []},
        "clue scroll (master)": {"npc_id": 13955, "metric": None, "tasks": []},
        "hellhound": {"npc_id": 100, "metric": "hellhound", "tasks": []},
    }

    @pytest.fixture
    def apply_effort(self, monkeypatch):
        recorded = []

        def _record(session, event_id, team_id, player_id, npc_norm, entry,
                    delta, *, source, at, completions=0, rolls=0):
            recorded.append({"npc": npc_norm, "delta": delta, "rolls": rolls})

        monkeypatch.setattr(engine, "record_effort", _record)
        redis = _KvRedis()

        def run(envelope, npcs=None):
            recorded.clear()
            state = _State(effort_npcs_by_event={1: npcs or self.NPCS})
            engine._apply_effort(None, redis, state, {"id": 1}, 9, 725,
                                 envelope, _NOW, {})
            return list(recorded)

        return run

    def _drop(self, npc, kc, item, quantity=1):
        return {"kind": "drop", "ts": 1000,
                "data": {"npc_name": npc, "kill_count": kc, "item_name": item,
                         "quantity": quantity}}

    def test_scroll_box_credits_the_tier_and_the_source_boss(self, apply_effort):
        recorded = apply_effort(self._drop("Hellhound", 1234, "Scroll box (hard)"))
        assert {(r["npc"], r["delta"], r["rolls"]) for r in recorded} == {
            ("clue scroll (hard)", 0, 1),   # the roll
            ("hellhound", 1, 0),            # still a Hellhound kill
        }

    def test_the_rest_of_that_kills_loot_does_not_re_credit_the_roll(self, apply_effort):
        apply_effort(self._drop("Hellhound", 1234, "Scroll box (hard)"))
        again = apply_effort(self._drop("Hellhound", 1234, "Ensouled hellhound head"))
        # Nothing at all: the roll is deduped on the source kill, and the kill
        # itself was already folded by the first envelope.
        assert sum(r["rolls"] for r in again) == 0

    def test_a_redelivered_envelope_does_not_re_credit_the_roll(self, apply_effort):
        apply_effort(self._drop("Hellhound", 1234, "Scroll box (hard)"))
        replay = apply_effort(self._drop("Hellhound", 1234, "Scroll box (hard)"))
        assert sum(r["rolls"] for r in replay) == 0

    def test_the_next_kill_rolls_again(self, apply_effort):
        apply_effort(self._drop("Hellhound", 1234, "Scroll box (hard)"))
        later = apply_effort(self._drop("Hellhound", 1240, "Scroll box (hard)"))
        assert sum(r["rolls"] for r in later) == 1

    def test_opening_a_casket_is_a_kill_not_a_roll(self, apply_effort):
        recorded = apply_effort(self._drop("Clue Scroll (Hard)", 55, "Dragon mace"))
        assert [(r["npc"], r["delta"], r["rolls"]) for r in recorded] == [
            ("clue scroll (hard)", 1, 0)]

    def test_a_master_from_an_elite_casket_is_both(self, apply_effort):
        """The one envelope that is genuinely two events: opening an elite
        casket that contained a master scroll."""
        recorded = apply_effort(
            self._drop("Clue Scroll (Elite)", 213, "Clue scroll (master)"))
        assert {(r["npc"], r["delta"], r["rolls"]) for r in recorded} == {
            ("clue scroll (master)", 0, 1),
            ("clue scroll (elite)", 1, 0),
        }

    def test_a_tier_the_event_does_not_track_earns_nothing(self, apply_effort):
        """Relevance is the same rule every other effort NPC obeys: no task
        cares about beginner clues here, so a beginner roll is not recorded."""
        recorded = apply_effort(
            self._drop("Hellhound", 1234, "Scroll box (beginner)"))
        assert [r["npc"] for r in recorded] == ["hellhound"]

    def test_an_absurd_quantity_is_clamped(self, apply_effort):
        recorded = apply_effort(
            self._drop("Hellhound", 1234, "Scroll box (hard)", quantity=4000))
        rolls = [r["rolls"] for r in recorded if r["npc"] == "clue scroll (hard)"]
        assert rolls == [engine._CLUE_ROLL_MAX_QUANTITY]

    def test_a_frozen_tier_stops_rolling(self, apply_effort, monkeypatch):
        monkeypatch.setattr(engine, "_effort_frozen", lambda *a, **k: True)
        recorded = apply_effort(self._drop("Hellhound", 1234, "Scroll box (hard)"))
        assert recorded == []


class TestScopeIsolation:
    def test_effort_scope_is_namespaced_away_from_credit_scopes(self):
        # A crediting kc_target scope is a bare task id or "id:npc"; effort
        # must never collide with one, or the two folds eat each other's
        # watermark and one of them silently stops counting.
        scope = effort_scope("Doom of Mokhaiotl")
        assert scope.startswith(f"{EFFORT_SCOPE_PREFIX}:")
        assert scope == "eff:doom_of_mokhaiotl"
        assert scope != engine._kc_state_scope({"id": 5, "kc_npcs": ["a", "b"]},
                                               "doom of mokhaiotl")

    def test_scope_normalizes_like_the_engine(self):
        assert effort_scope("  YAMA  ") == effort_scope("yama")


class _FakeRedis:
    def __init__(self, sets=None):
        self.sets = sets or {}

    def smembers(self, key):
        return self.sets.get(key, set())


#: Any datetime works — record_effort is stubbed out in these tests.
_NOW = datetime(2026, 8, 28, 12, 0, 0)


class _State:
    """Stand-in for MatcherState: _apply_effort only reads one attribute."""

    def __init__(self, effort_npcs_by_event):
        self.effort_npcs_by_event = effort_npcs_by_event


class _KvRedis(_FakeRedis):
    """Enough of a Redis for the watermark fold: strings + sets."""

    def __init__(self, sets=None):
        super().__init__(sets)
        self.kv = {}

    def get(self, key):
        v = self.kv.get(key)
        return None if v is None else str(v).encode()

    def set(self, key, value, ex=None):
        self.kv[key] = value

    def incr(self, key):
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    def expire(self, key, ttl):
        return True

    def delete(self, key):
        self.kv.pop(key, None)

    def sadd(self, key, member):
        bucket = self.sets.setdefault(key, set())
        if member in bucket:
            return 0
        bucket.add(member)
        return 1

    def sismember(self, key, member):
        return member in self.sets.get(key, set())


class TestApplyEffortRouting:
    """Which counter an envelope feeds at a partial-credit NPC.

    The read-model split is only as good as what the fold writes: WOM's metric
    must reach ``completions`` and never the attempt counter (that conflation
    IS report #131), and the marker drop must credit a completion exactly once
    per attempt even though the same chest sends a dozen other items.
    """

    NPCS = {"fortis colosseum": {"npc_id": 13741, "metric": "sol_heredit",
                                 "tasks": []}}

    @pytest.fixture
    def apply_effort(self, monkeypatch):
        """Drive ``_apply_effort`` and return what it asked record_effort for.

        Redis is shared across calls within a test so the dedupe and watermark
        state carry, the way they do for one player in one event.
        """
        recorded = []

        def _record(session, event_id, team_id, player_id, npc_norm, entry,
                    delta, *, source, at, completions=0, rolls=0):
            recorded.append({"npc": npc_norm, "delta": delta,
                             "completions": completions, "rolls": rolls,
                             "source": source})

        monkeypatch.setattr(engine, "record_effort", _record)
        redis = _KvRedis()

        def run(envelope, npcs=None):
            recorded.clear()
            state = _State(effort_npcs_by_event={1: npcs or self.NPCS})
            engine._apply_effort(None, redis, state, {"id": 1}, 9, 725,
                                 envelope, _NOW, {})
            return list(recorded)

        return run

    def _drop(self, kc, item):
        return {"kind": "drop", "ts": 1000,
                "data": {"npc_name": "Fortis Colosseum", "kill_count": kc,
                         "item_name": item}}

    def test_wom_metric_credits_completions_and_no_attempts(self, apply_effort):
        # The reconciler's shape: lifetime kc 12, window-start seed 3, so nine
        # completions happened inside the event.
        recorded = apply_effort({
            "kind": "wom_kc",
            "data": {"boss_metric": "sol_heredit", "kc": 12, "kc_start": 3,
                     "target_event_id": 1},
        })
        assert len(recorded) == 1
        # 0 attempts: the plugin's chest KC already counted them. Crediting
        # both is what made one counter's lifetime lead land as in-event kills.
        assert recorded[0]["delta"] == 0
        assert recorded[0]["completions"] == 9
        assert recorded[0]["source"] == engine.KC_SOURCE_WOM

    def test_wom_completions_never_reach_the_attempt_watermark(self, apply_effort):
        """The report #131 shape, at the NPC that caused it.

        A lifetime sol_heredit of 172 arriving after the plugin has reported
        920 attempts must not credit the difference — under the old single
        watermark it credited 748.
        """
        apply_effort(self._drop(920, "Sunfire splinters"))
        recorded = apply_effort({
            "kind": "wom_kc",
            "data": {"boss_metric": "sol_heredit", "kc": 172},
        })
        assert all(r["delta"] == 0 for r in recorded)
        assert sum(r["completions"] for r in recorded) == 0  # first WOM sighting = baseline

    def test_marker_drop_credits_one_completion_per_attempt(self, apply_effort):
        first = apply_effort(self._drop(921, "Dizana's quiver (uncharged)"))
        assert sum(r["completions"] for r in first) == 1
        # The same chest's other items must not re-credit it...
        again = apply_effort(self._drop(921, "Sunfire splinters"))
        assert sum(r["completions"] for r in again) == 0
        # ...nor may a redelivery of the quiver envelope itself.
        replay = apply_effort(self._drop(921, "Dizana's quiver (uncharged)"))
        assert sum(r["completions"] for r in replay) == 0

    def test_a_bail_credits_an_attempt_and_no_completion(self, apply_effort):
        recorded = apply_effort(self._drop(922, "Sunfire splinters"))
        assert len(recorded) == 1
        assert recorded[0]["delta"] == 1
        assert recorded[0]["completions"] == 0

    def test_ordinary_npc_never_touches_the_completion_counter(self, apply_effort):
        recorded = apply_effort(
            {"kind": "drop", "ts": 1000,
             "data": {"npc_name": "Zulrah", "kill_count": 500,
                      "item_name": "Dizana's quiver (uncharged)"}},
            npcs={"zulrah": {"npc_id": 2042, "metric": "zulrah", "tasks": []}},
        )
        assert [r["completions"] for r in recorded] == [0]


class TestFreeze:
    def test_frozen_once_every_task_the_npc_feeds_is_done(self):
        r = _FakeRedis({engine._done_tasks_key(1, 9): {b"4", b"5"}})
        assert engine._effort_frozen(r, 1, 9, [4, 5], {}) is True

    def test_not_frozen_while_any_task_is_still_open(self):
        # Brondt's case in reverse: a boss feeding two tiles keeps counting
        # until BOTH are done.
        r = _FakeRedis({engine._done_tasks_key(1, 9): {b"4"}})
        assert engine._effort_frozen(r, 1, 9, [4, 5], {}) is False

    def test_npc_with_no_tasks_never_freezes(self):
        assert engine._effort_frozen(_FakeRedis(), 1, 9, [], {}) is False

    def test_done_set_is_read_once_per_envelope(self):
        calls = []

        class Counting(_FakeRedis):
            def smembers(self, key):
                calls.append(key)
                return {b"4"}

        cache = {}
        r = Counting()
        engine._effort_frozen(r, 1, 9, [4], cache)
        engine._effort_frozen(r, 1, 9, [4], cache)
        assert len(calls) == 1

    def test_redis_failure_reads_as_not_frozen(self):
        # Fail open: a Redis hiccup must under-freeze (keep counting), never
        # silently stop recording a player's work.
        class Broken(_FakeRedis):
            def smembers(self, key):
                raise RuntimeError("redis down")

        assert engine._effort_frozen(Broken(), 1, 9, [4], {}) is False
