"""Suggestion #256: the standing clan points board."""
import importlib.util
import os
import sys
from datetime import datetime

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "points_board_discord.py",
)
_spec = importlib.util.spec_from_file_location("_points_board_under_test", _PATH)
pb = importlib.util.module_from_spec(_spec)
sys.modules["_points_board_under_test"] = pb
_spec.loader.exec_module(pb)


def test_size_defaults_and_clamps():
    assert pb.board_size(None) == 10
    assert pb.board_size("abc") == 10
    assert pb.board_size("1") == 3
    assert pb.board_size("99") == 25
    assert pb.board_size("15") == 15


def test_period_defaults_to_season():
    assert pb.board_period(None) == "season"
    assert pb.board_period("bogus") == "season"
    assert pb.board_period("ALL") == "all"


def test_current_season_picks_the_running_one():
    now = datetime(2026, 11, 20)
    seasons = [
        {"id": 1, "start_at": datetime(2026, 1, 1), "end_at": datetime(2026, 6, 1)},
        {"id": 2, "start_at": datetime(2026, 11, 1), "end_at": None},
        {"id": 3, "start_at": datetime(2026, 12, 1), "end_at": None},
    ]
    assert pb.current_season(seasons, now)["id"] == 2
    assert pb.current_season(seasons[:1], now) is None


def test_message_ref_round_trip():
    assert pb.parse_message_ref("123:456") == ("123", "456")
    assert pb.parse_message_ref("") == (None, None)
    assert pb.parse_message_ref("123") == (None, None)


def test_hash_moves_with_channel_and_content():
    embed = {"title": "x", "description": "a"}
    assert pb.board_hash("1", embed) == pb.board_hash("1", dict(embed))
    assert pb.board_hash("1", embed) != pb.board_hash("2", embed)
    assert pb.board_hash("1", embed) != pb.board_hash("1", {"title": "x", "description": "b"})
