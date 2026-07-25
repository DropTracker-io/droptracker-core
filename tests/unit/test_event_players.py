"""Unit tests for the pure event-players leaderboard helpers
(web_api/event_players.py). No DB — inputs are the already-fetched rollups."""
from __future__ import annotations

from web_api.event_players import (
    count_contributions,
    is_metric_task,
    norm_item_name,
    rank_players,
    task_contributions,
    top_items,
)


def test_norm_item_name_collapses_case_and_whitespace():
    assert norm_item_name("  Twisted   Bow ") == "twisted bow"
    assert norm_item_name(None) == ""


def test_top_items_orders_by_quantity_and_attaches_ids():
    rows = [
        {"name": "Coins", "quantity": 1, "drops": 1},
        {"name": "Twisted bow", "quantity": 2, "drops": 2},
        {"name": "Dragon claws", "quantity": 9, "drops": 1},
    ]
    item_ids = {"twisted bow": 20997, "dragon claws": 13652}  # Coins unknown
    out = top_items(rows, item_ids, limit=2)
    assert [r["name"] for r in out] == ["Dragon claws", "Twisted bow"]  # qty desc, capped
    assert out[0]["item_id"] == 13652
    assert out[1]["item_id"] == 20997


def test_top_items_unknown_name_gets_none_id():
    out = top_items([{"name": "Made-up item", "quantity": 1, "drops": 1}], {}, limit=5)
    assert out[0]["item_id"] is None


def test_rank_players_sorts_by_points_then_completions():
    contrib = {
        1: {"completions": 3, "quantity": 5, "tasks": 2},
        2: {"completions": 3, "quantity": 5, "tasks": 2},
        3: {"completions": 1, "quantity": 1, "tasks": 1},
    }
    points = {1: 40.0, 2: 40.0, 3: 100.0}
    membership = {
        1: {"team_id": 7, "team_name": "Alpha", "team_color": "#fff", "role": "leader"},
        2: {"team_id": 7, "team_name": "Alpha", "team_color": "#fff", "role": None},
    }
    names = {1: "Zezima", 2: "Alpha", 3: "Woox"}
    items = {1: [{"name": "Twisted bow", "item_id": 20997, "quantity": 1, "drops": 1}]}
    rows = rank_players(contrib, points, membership, names, items)
    # Woox first (most points); then the two tied on points/completions break by name.
    assert [r["player_id"] for r in rows] == [3, 2, 1]
    assert rows[0]["points"] == 100.0 and rows[0]["team_id"] is None  # no membership row
    assert rows[2]["role"] == "leader"
    assert rows[2]["items"] == items[1]


def test_rank_players_includes_points_only_and_ledger_only_players():
    # A player with split points but no ledger rollup, and vice versa, both show.
    contrib = {1: {"completions": 2, "quantity": 2, "tasks": 1}}
    points = {2: 10.0}
    rows = rank_players(contrib, points, {}, {1: "A", 2: "B"}, {})
    ids = {r["player_id"] for r in rows}
    assert ids == {1, 2}
    b = next(r for r in rows if r["player_id"] == 2)
    assert b["completions"] == 0 and b["points"] == 10.0


def test_rank_players_missing_name_falls_back():
    rows = rank_players({5: {"completions": 1, "quantity": 1, "tasks": 1}}, {}, {}, {}, {})
    assert rows[0]["player_name"] == "Player 5"


def test_rank_players_includes_rostered_players_without_contributions():
    # A rostered player with no ledger rows / points still gets a row (their
    # event-window loot GP is meaningful before they score).
    membership = {9: {"team_id": 1, "team_name": "Alpha", "team_color": None,
                      "role": None}}
    rows = rank_players({}, {}, membership, {9: "Newbie"}, {},
                        loot_gp={9: 1_500_000})
    assert len(rows) == 1
    assert rows[0]["player_id"] == 9
    assert rows[0]["points"] == 0.0 and rows[0]["completions"] == 0
    assert rows[0]["loot_gp"] == 1_500_000


def test_rank_players_gp_breaks_ties_before_name():
    contrib = {}
    membership = {1: {"team_id": 1}, 2: {"team_id": 1}}
    rows = rank_players(contrib, {}, membership, {1: "Aaa", 2: "Zzz"}, {},
                        loot_gp={1: 100, 2: 900})
    # Equal points/completions/quantity -> higher GP first despite name order.
    assert [r["player_id"] for r in rows] == [2, 1]


def test_rank_players_defaults_gp_to_zero_without_map():
    rows = rank_players({1: {"completions": 1, "quantity": 1, "tasks": 1}},
                        {}, {}, {}, {})
    assert rows[0]["loot_gp"] == 0


# --- Contribution counting (metric tasks collapse their update spam) ---------


def test_metric_tasks_collapse_to_one_contribution():
    # 50 kills toward one kc task, 12 xp updates toward one xp task: each run
    # is a single ongoing contribution, not 50 / 12 of them.
    assert task_contributions("kc_target", 50) == 1
    assert task_contributions("xp_target", 12) == 1
    assert task_contributions("loot_value", 7) == 1
    assert task_contributions("skill_target", 3) == 1


def test_acquisition_tasks_count_every_ledger_row():
    # Three items pulled on three different nights is three contributions.
    assert task_contributions("item_collection", 3) == 3
    assert task_contributions("pet_collection", 2) == 2
    assert task_contributions("pb_target", 4) == 4
    assert task_contributions("loot_sweep", 9) == 9


def test_task_contributions_zero_rows_is_zero():
    assert task_contributions("kc_target", 0) == 0
    assert task_contributions("item_collection", 0) == 0
    assert task_contributions("kc_target", -1) == 0


def test_unknown_task_type_counts_per_row():
    # An unmapped / missing type must not silently swallow contributions.
    assert task_contributions(None, 4) == 4
    assert task_contributions("brand_new_type", 4) == 4
    assert is_metric_task("brand_new_type") is False


def test_count_contributions_mixes_task_types():
    # 40 kc rows on task 1 -> 1; 3 item rows on task 2 -> 3; 9 xp rows -> 1.
    rows_by_task = {1: 40, 2: 3, 3: 9}
    task_types = {1: "kc_target", 2: "item_collection", 3: "xp_target"}
    assert count_contributions(rows_by_task, task_types) == 5


def test_count_contributions_empty():
    assert count_contributions({}, {}) == 0
