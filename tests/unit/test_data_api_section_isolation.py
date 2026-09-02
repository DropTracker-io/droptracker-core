"""One player's bad row must not cost the whole page its section.

Every loader reads the page in a single query, so an exception raised while
walking the result set aborts it for all 100 players at once. That is how a
roster came back with `personal_bests: {"error": "unavailable"}` on every
member when a single legacy row — a PB with a NULL npc_id, written before the
writers resolved the NPC first — was somewhere in the page.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sect = _load("_real_sections", "data_api/sections.py")


class _Session:
    """Just enough session for a loader: one canned result set per execute."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.rollbacks = 0
        self.queries = 0

    def execute(self, _statement):
        self.queries += 1
        return list(self.rows)

    def rollback(self):
        self.rollbacks += 1


def _section(loader, key="probe", cost=1):
    return sect.Section(key=key, cost=cost, loader=loader, description="")


class TestOneBadPlayerDoesNotBreakThePage:
    def test_only_the_failing_player_is_marked_unavailable(self):
        def loader(_session, player_ids, _ctx):
            if 1593 in player_ids:
                raise TypeError("int() argument must not be NoneType")
            return {pid: {"bests": [pid]} for pid in player_ids}

        sect.REGISTRY["probe"] = _section(loader)
        try:
            merged = sect.load_sections(_Session(), ["probe"],
                                        [1190, 1593, 1699], {})
        finally:
            del sect.REGISTRY["probe"]

        assert merged[1593]["probe"] == {"error": "unavailable"}
        assert merged[1190]["probe"] == {"bests": [1190]}
        assert merged[1699]["probe"] == {"bests": [1699]}

    def test_a_single_player_request_still_reports_unavailable(self):
        def loader(_session, _player_ids, _ctx):
            raise ValueError("boom")

        sect.REGISTRY["probe"] = _section(loader)
        try:
            merged = sect.load_sections(_Session(), ["probe"], [1593], {})
        finally:
            del sect.REGISTRY["probe"]
        assert merged[1593]["probe"] == {"error": "unavailable"}

    def test_the_page_query_is_tried_once_before_falling_back(self):
        calls = []

        def loader(_session, player_ids, _ctx):
            calls.append(list(player_ids))
            if len(player_ids) > 1:
                raise TypeError("one bad row")
            return {player_ids[0]: {"ok": True}}

        sect.REGISTRY["probe"] = _section(loader)
        try:
            sect.load_sections(_Session(), ["probe"], [1, 2, 3], {})
        finally:
            del sect.REGISTRY["probe"]
        # One page attempt, then one attempt per player — not a per-player
        # loop in the healthy case, which would be an N+1 on every request.
        assert calls == [[1, 2, 3], [1], [2], [3]]

    def test_a_healthy_section_runs_exactly_one_query(self):
        calls = []

        def loader(_session, player_ids, _ctx):
            calls.append(list(player_ids))
            return {pid: {"ok": True} for pid in player_ids}

        sect.REGISTRY["probe"] = _section(loader)
        try:
            merged = sect.load_sections(_Session(), ["probe"], [1, 2, 3], {})
        finally:
            del sect.REGISTRY["probe"]
        assert calls == [[1, 2, 3]]
        assert all(merged[pid]["probe"] == {"ok": True} for pid in (1, 2, 3))

    def test_a_statement_timeout_is_never_retried_per_player(self, monkeypatch):
        # The server said the work is too heavy; running it 100 more times is
        # the opposite of the right response. It must surface as the 503.
        import data_api.core as core

        calls = []

        class _Timeout(Exception):
            def __init__(self):
                self.orig = type("orig", (), {"args": (1969, "timed out")})()

        def loader(_session, player_ids, _ctx):
            calls.append(list(player_ids))
            raise _Timeout()

        sect.REGISTRY["probe"] = _section(loader)
        try:
            with pytest.raises(_Timeout):
                sect.load_sections(_Session(), ["probe"], [1, 2, 3], {})
        finally:
            del sect.REGISTRY["probe"]
        assert calls == [[1, 2, 3]], "a timeout must not fan out into N queries"

    def test_other_sections_are_unaffected_by_a_failing_one(self):
        def broken(_session, _player_ids, _ctx):
            raise TypeError("bad row")

        def fine(_session, player_ids, _ctx):
            return {pid: {"value": pid} for pid in player_ids}

        sect.REGISTRY["broken"] = _section(broken, key="broken")
        sect.REGISTRY["fine"] = _section(fine, key="fine")
        try:
            merged = sect.load_sections(_Session(), ["broken", "fine"], [7], {})
        finally:
            del sect.REGISTRY["broken"], sect.REGISTRY["fine"]
        assert merged[7]["broken"] == {"error": "unavailable"}
        assert merged[7]["fine"] == {"value": 7}


class TestPersonalBestsSurvivesAPbWithNoNpc:
    """The row that started it: a real time attached to no boss."""

    def test_a_null_npc_id_row_does_not_raise(self):
        # Fed straight to the loop, bypassing the WHERE clause: the row
        # handling has to hold on its own, or the SQL filter is the only
        # thing between a nullable column and a page-wide failure.
        session = _Session([
            (1593, None, "2", 1366800),      # legacy row, no boss
            (1593, 415, "Solo", 92400),
        ])
        out = sect._load_personal_bests(session, [1593], {})
        assert out[1593]["bests"] == [
            {"npc_id": 415, "team_size": "Solo", "best_ms": 92400}
        ]

    def test_a_player_whose_only_pb_is_unattributable_is_not_an_error(self):
        # Empty bests, not a failure: the player is fine, the row is not.
        session = _Session([(5213, None, "2", 3090000)])
        assert sect._load_personal_bests(session, [5213], {}) == {
            5213: {"bests": []}
        }

    def test_the_query_excludes_them_rather_than_relying_on_the_loop(self):
        # Filtering in SQL keeps the unreportable rows off the wire instead of
        # fetching and discarding them.
        source = (_ROOT / "data_api" / "sections.py").read_text()
        body = source.split("def _load_personal_bests(", 1)[1].split("\ndef ", 1)[0]
        assert "npc_id IS NOT NULL" in body
