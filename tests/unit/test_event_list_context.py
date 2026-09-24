"""GET /events list-card context: group name + team/player counts.

The staff overview (/admin/events) labels each row with its group's name and
roster size. Pinned here: the three lookups are batched (one query each for the
whole list, never per row), and events with no teams read as zero, not absent.
"""
from __future__ import annotations

import web_api.routes.events as evr

from tests.unit.test_event_auth_modes import _S


def test_group_names_and_counts_are_batched():
    rows = [{"id": 1, "group_id": 7}, {"id": 2, "group_id": None}, {"id": 3, "group_id": 7}]
    s = _S(
        [(7, "Realists")],     # group names
        [(1, 4), (3, 2)],      # teams per event
        [(1, 38)],             # players per event
    )
    evr._add_list_context(s, rows)
    assert s._batches == []  # exactly three queries
    assert rows[0] == {"id": 1, "group_id": 7, "group_name": "Realists",
                       "team_count": 4, "player_count": 38}
    assert rows[1]["group_name"] is None
    assert (rows[1]["team_count"], rows[1]["player_count"]) == (0, 0)
    assert (rows[2]["team_count"], rows[2]["player_count"]) == (2, 0)


def test_global_only_list_skips_the_group_query():
    rows = [{"id": 5, "group_id": None}]
    s = _S([], [])  # teams, players — no group-name query
    evr._add_list_context(s, rows)
    assert s._batches == []
    assert rows[0]["group_name"] is None


def test_empty_list_issues_no_queries():
    evr._add_list_context(_S(), [])
