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

import pytest

from services.event_effort import (
    EFFORT_SCOPE_PREFIX,
    build_effort_map,
    effort_scope,
    ehb_hours,
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
