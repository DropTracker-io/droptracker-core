"""Unit tests for the pure event-players leaderboard helpers
(web_api/event_players.py). No DB — inputs are the already-fetched rollups."""
from __future__ import annotations

from web_api.event_players import norm_item_name, rank_players, top_items


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
