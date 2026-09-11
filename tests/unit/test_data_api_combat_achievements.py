"""The ``combat_achievements`` section: a player's points and the tier they reach.

Pinned without a database: the loader's shaping (every null means "unknown",
never an invented zero), where the thresholds come from, and that the page is
still one query. What writes ``points`` is covered by test_ca_points.py.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sect = _load("_real_sections", "data_api/sections.py")

CURRENT = {"Easy": 41, "Medium": 169, "Hard": 436, "Elite": 1100,
           "Master": 1965, "Grandmaster": 2697}
WHEN = datetime(2026, 9, 10, 12, 0, 0)


class _Session:
    """One canned result set: (player_id, tasks_completed, points, updated_at)."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = 0

    def execute(self, _statement):
        self.queries += 1
        return list(self.rows)


def _load_ca(rows, ctx=None, ids=None):
    session = _Session(rows)
    ids = ids or [r[0] for r in rows]
    return sect._load_combat_achievements(session, ids, {"ca_thresholds": CURRENT}
                                          if ctx is None else ctx), session


class TestShape:
    def test_points_tier_and_progress(self):
        out, _ = _load_ca([(1593, 231, 656, WHEN)])
        assert out[1593] == {
            "tasks_completed": 231,
            "points": 656,
            "tier": "Hard",
            "next_tier": "Elite",
            "next_tier_points": 1100,
            "points_to_next": 444,
            "progress": 33.13,
            "updated_at": "2026-09-10T12:00:00",
        }

    def test_a_player_known_only_from_completions_has_points_but_no_task_count(self):
        # Completed tasks since installing the plugin, never synced: the
        # game's total is known, the task count is not.
        out, _ = _load_ca([(77, None, 1200, WHEN)])
        assert out[77]["tasks_completed"] is None
        assert out[77]["points"] == 1200
        assert out[77]["tier"] == "Elite"

    def test_no_total_yet_is_null_throughout_not_zero(self):
        out, _ = _load_ca([(5, 12, None, WHEN)])
        entry = out[5]
        assert entry["tasks_completed"] == 12
        assert [entry[k] for k in ("points", "tier", "next_tier", "next_tier_points",
                                   "points_to_next", "progress")] == [None] * 6

    def test_zero_points_is_no_tier_yet(self):
        out, _ = _load_ca([(9, 0, 0, WHEN)])
        assert out[9]["tier"] is None
        assert out[9]["next_tier"] == "Easy"
        assert out[9]["points_to_next"] == 41

    def test_grandmaster(self):
        out, _ = _load_ca([(1, 655, 2697, WHEN)])
        assert out[1]["tier"] == "Grandmaster"
        assert out[1]["next_tier"] is None
        assert out[1]["points_to_next"] == 0
        assert out[1]["progress"] == 100.0

    def test_a_player_with_no_row_is_absent(self):
        # load_sections turns "absent" into a null section for that player.
        out, _ = _load_ca([], ids=[123])
        assert out == {}

    def test_the_payload_is_json(self):
        out, _ = _load_ca([(1593, 231, 656, WHEN)])
        assert json.loads(json.dumps(out[1593]))["points"] == 656


class TestThresholds:
    def test_the_request_s_thresholds_are_used(self):
        # A lower Elite threshold moves the same total up a tier.
        ctx = {"ca_thresholds": dict(CURRENT, Elite=600)}
        out, _ = _load_ca([(1, None, 656, WHEN)], ctx=ctx)
        assert out[1]["tier"] == "Elite"

    def test_without_them_the_process_s_own_table_is_used(self):
        from services.ca_tiers import FALLBACK_TIER_POINTS, reset_cache

        reset_cache()
        out, _ = _load_ca([(1, None, 656, WHEN)], ctx={})
        elite = FALLBACK_TIER_POINTS["Elite"]
        assert out[1]["next_tier_points"] == elite


class TestCost:
    def test_the_whole_page_is_still_one_query(self):
        _, session = _load_ca([(1, 1, 10, WHEN), (2, 2, 500, WHEN), (3, None, 900, WHEN)])
        assert session.queries == 1

    def test_the_section_stays_cheap_and_in_all(self):
        # The tier is arithmetic on a column already read; nothing new is
        # queried, so the measured weight does not move.
        assert sect.REGISTRY["combat_achievements"].cost == 1
        assert "combat_achievements" in sect.parse_include("all")
