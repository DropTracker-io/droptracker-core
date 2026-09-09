"""The site's personal-best boards name each entry's loadout (web110a).

``/personal-bests/board`` used to say only who and how fast. To offer "show
gear" the site needs the row id to fetch it by and a promise that there is
something to fetch — most times predate gear capture, and a button that opens
onto "No gear recorded" on nine entries in ten is worse than no button.

``_build_dataset`` is driven against a stubbed session so the per-player
"fastest row wins" rule is pinned for the new fields too: the pb_id named must
be the fastest row's, not the first one the query happened to return.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import web_api.routes.personal_bests as pb

ZULRAH = 2042
T0 = datetime(2026, 9, 1, 12, 0)


def row(pb_id, pid, ms, *, has_loadout=0, ts="Solo", when=T0, image=None):
    # Column order of the SELECT in _build_dataset.
    return (pb_id, ZULRAH, "Zulrah", ts, pid, ms, when, image, has_loadout)


@pytest.fixture
def build(monkeypatch):
    def _build(rows, hidden=()):
        @contextmanager
        def _session():
            s = MagicMock()
            s.execute.return_value.fetchall.return_value = rows
            yield s

        monkeypatch.setattr(pb, "db_session", _session)
        monkeypatch.setattr(pb, "hidden_player_ids", lambda: set(hidden))
        return pb._build_dataset(None)

    return _build


def solo_entries(dataset):
    return dataset[ZULRAH]["boards"]["Solo"]


class TestLoadoutFlag:
    def test_entries_name_their_row_and_whether_gear_was_captured(self, build):
        entries = solo_entries(build([row(11, 1, 60_000, has_loadout=1), row(12, 2, 61_000)]))
        assert [(e["pb_id"], e["has_loadout"]) for e in entries] == [(11, True), (12, False)]

    def test_the_fastest_rows_id_is_the_one_named(self, build):
        # Two rows for the same player on the same board (legacy duplicates):
        # the loadout offered must be the one attached to the time shown.
        entries = solo_entries(build([
            row(21, 1, 65_000, has_loadout=1),
            row(22, 1, 60_000, has_loadout=0),
        ]))
        assert len(entries) == 1
        assert entries[0]["time_ms"] == 60_000
        assert (entries[0]["pb_id"], entries[0]["has_loadout"]) == (22, False)

    def test_the_database_truthiness_of_the_join_is_normalised(self, build):
        # MariaDB answers the IS NOT NULL expression as 0/1, not a bool.
        entries = solo_entries(build([row(31, 1, 60_000, has_loadout=1)]))
        assert entries[0]["has_loadout"] is True
        assert entries[0]["pb_id"] == 31


class TestExistingRulesStillHold:
    def test_hidden_players_are_dropped(self, build):
        entries = solo_entries(build([row(1, 1, 60_000), row(2, 2, 61_000)], hidden=[1]))
        assert [e["player_id"] for e in entries] == [2]

    def test_a_screenshot_we_do_not_host_is_not_offered(self, build):
        entries = solo_entries(build([row(1, 1, 60_000, image="https://cdn.discordapp.com/x.png")]))
        assert entries[0]["image_url"] is None


class TestBossIndexRecord:
    """The index card's record block opens the record holder's gear in place."""

    def _info(self, dataset):
        return dataset[ZULRAH]

    def test_the_record_is_the_fastest_head_across_boards_with_its_team_size(self, build):
        info = self._info(build([
            row(1, 1, 65_000, has_loadout=1, ts="Solo"),
            row(2, 2, 60_000, has_loadout=0, ts="2"),
        ]))
        fastest = pb._fastest_on_any_board(info)
        assert (fastest["pb_id"], fastest["team_size"], fastest["time_ms"]) == (2, "2", 60_000)

    def test_the_record_block_names_the_row_and_its_loadout_like_a_board_entry(self, build):
        info = self._info(build([row(7, 1, 60_000, has_loadout=1)]))
        payload = pb._record_payload(pb._fastest_on_any_board(info), {1: "Ashey"})
        assert payload == {
            "time_ms": 60_000,
            "time_display": pb._convert_from_ms(60_000),
            "team_size": "Solo",
            "player_id": 1,
            "player_name": "Ashey",
            "pb_id": 7,
            "has_loadout": True,
        }

    def test_an_unknown_name_and_no_loadout_still_serialise(self, build):
        info = self._info(build([row(7, 1, 60_000)]))
        payload = pb._record_payload(pb._fastest_on_any_board(info), {})
        assert (payload["player_name"], payload["has_loadout"]) == ("Unknown", False)
