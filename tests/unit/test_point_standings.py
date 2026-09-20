"""Who stands where on a clan's points board (db/point_standings.py).

Two rules are decided there and nowhere else, so both are pinned here against
a real engine -- SQLite -- rather than a scripted session:

* only CURRENT members stand. ``player_points`` is never edited when somebody
  leaves; what disappears is their ``user_group_association`` row, and the
  board has to follow that without deleting anything;
* a group may combine a Discord user's RSNs into one standing, and the two
  rules have to compose: the alt that left stops counting, the main that
  stayed keeps its place.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# By name, never ``from db import point_standings``: conftest stubs the ``db``
# package with a MagicMock, whose attribute lookup would hand back a mock.
from db.point_standings import (
    COMBINE_CONFIG_KEY,
    MemberTotal,
    build_standings,
    combine_enabled,
    counted_player_ids,
    display_total,
    find_standing,
    load_member_totals,
    load_seasons,
    load_standings,
    member_player_ids,
    page_of,
    paginate,
    recent_awards,
    resolve_period,
    search_standings,
    visible_standings,
)

GROUP = 190
OTHER_GROUP = 7


def _row(pid, name, points, user_id=None, hidden=False):
    return MemberTotal(player_id=pid, name=name, user_id=user_id, hidden=hidden, points=points)


# ── build_standings ──────────────────────────────────────────────────────────

class TestBuildStandings:
    def test_per_rsn_board_ranks_each_account_alone(self):
        board = build_standings(
            [_row(1, "Main", 50, user_id=9), _row(2, "Alt", 80, user_id=9), _row(3, "Solo", 60)],
            combine=False,
        )
        assert [(s.rank, s.primary.name, s.points) for s in board] == [
            (1, "Alt", 80), (2, "Solo", 60), (3, "Main", 50),
        ]
        assert all(not s.combined and s.user_id is None for s in board)

    def test_combined_board_sums_a_users_accounts(self):
        board = build_standings(
            [_row(1, "Main", 50, user_id=9), _row(2, "Alt", 80, user_id=9), _row(3, "Solo", 100)],
            combine=True,
        )
        assert [(s.rank, s.points) for s in board] == [(1, 130), (2, 100)]
        top = board[0]
        assert top.user_id == 9 and top.combined
        # Shown under the account that earned the most; both are listed.
        assert top.primary.name == "Alt"
        assert [(a.name, a.points) for a in top.accounts] == [("Alt", 80), ("Main", 50)]

    def test_unclaimed_players_never_merge(self):
        # user_id None is "nobody claimed this", not a shared owner.
        board = build_standings([_row(1, "A", 10), _row(2, "B", 10)], combine=True)
        assert len(board) == 2 and all(s.user_id is None for s in board)

    def test_user_id_zero_is_a_real_owner(self):
        # users.user_id 0 is a real, very active account; a truthiness test
        # would silently leave their alts un-merged.
        board = build_standings(
            [_row(1, "A", 10, user_id=0), _row(2, "B", 5, user_id=0)], combine=True
        )
        assert len(board) == 1
        assert board[0].user_id == 0 and board[0].points == 15

    def test_zero_totals_do_not_stand_but_negative_ones_do(self):
        board = build_standings(
            [_row(1, "Zero", 0), _row(2, "Debt", -5), _row(3, "Up", 5)], combine=False
        )
        assert [(s.primary.name, s.points) for s in board] == [("Up", 5), ("Debt", -5)]

    def test_combined_entry_that_nets_to_zero_is_dropped(self):
        board = build_standings(
            [_row(1, "Main", 10, user_id=9), _row(2, "Alt", -10, user_id=9)], combine=True
        )
        assert board == []

    def test_ties_break_on_lowest_player_id(self):
        board = build_standings([_row(8, "Late", 10), _row(3, "Early", 10)], combine=False)
        assert [s.primary.name for s in board] == ["Early", "Late"]

    def test_hidden_account_counts_but_is_never_named(self):
        board = build_standings(
            [_row(1, "Main", 50, user_id=9), _row(2, "Secret", 80, user_id=9, hidden=True)],
            combine=True,
        )
        entry = board[0]
        assert entry.points == 130
        assert entry.primary.name == "Main"
        assert [a.name for a in entry.visible_accounts] == ["Main"]
        assert not entry.hidden

    def test_fully_hidden_entry_leaves_a_rank_gap(self):
        board = build_standings(
            [_row(1, "Ghost", 90, hidden=True), _row(2, "Seen", 10)], combine=False
        )
        shown = visible_standings(board)
        assert [(s.rank, s.primary.name) for s in shown] == [(2, "Seen")]


# ── search / find / paginate ─────────────────────────────────────────────────

class TestSearchAndPaging:
    def _board(self):
        return build_standings(
            [
                _row(1, "Tzuk Kal Lag", 90),
                _row(2, "Main", 50, user_id=9),
                _row(3, "Iron Alt", 30, user_id=9),
                _row(4, "Secret", 20, hidden=True),
            ],
            combine=True,
        )

    def test_search_keeps_the_rank_held_on_the_full_board(self):
        found = search_standings(self._board(), "main")
        assert [(s.rank, s.primary.name) for s in found] == [(2, "Main")]

    def test_search_matches_any_combined_account(self):
        found = search_standings(self._board(), "iron")
        assert [s.primary.name for s in found] == ["Main"]

    def test_search_folds_osrs_separators(self):
        assert [s.rank for s in search_standings(self._board(), "tzuk-kal")] == [1]

    def test_search_never_confirms_a_hidden_name(self):
        assert search_standings(self._board(), "secret") == []

    def test_blank_query_is_no_filter(self):
        board = self._board()
        assert search_standings(board, "  ") == board
        assert search_standings(board, None) == board

    def test_find_by_player_covers_combined_alts(self):
        board = self._board()
        assert find_standing(board, player_id=3).rank == 2
        assert find_standing(board, user_id=9).rank == 2
        assert find_standing(board, player_id=99) is None

    def test_find_by_user_id_zero(self):
        board = build_standings([_row(1, "A", 10, user_id=0)], combine=True)
        assert find_standing(board, user_id=0).points == 10

    def test_paginate_clamps_a_stale_page(self):
        items, page, pages = paginate(list(range(25)), 9, 10)
        assert (items, page, pages) == ([20, 21, 22, 23, 24], 3, 3)

    def test_paginate_empty_board_is_one_empty_page(self):
        assert paginate([], 1, 10) == ([], 1, 1)

    def test_page_of_locates_an_entry(self):
        board = build_standings([_row(i, f"P{i}", 100 - i) for i in range(1, 26)], combine=False)
        assert page_of(board, board[0], 10) == 1
        assert page_of(board, board[10], 10) == 2
        assert page_of(board, board[24], 10) == 3


# ── resolve_period ───────────────────────────────────────────────────────────

class TestResolvePeriod:
    NOW = datetime(2026, 9, 18, 15, 30)  # a Friday

    def test_all_time_has_no_bounds(self):
        assert resolve_period("all", self.NOW) == ("all", None, None)

    def test_month_is_the_default(self):
        expected = ("202609", datetime(2026, 9, 1), datetime(2026, 10, 1))
        assert resolve_period("month", self.NOW) == expected
        assert resolve_period(None, self.NOW) == expected
        assert resolve_period("nonsense", self.NOW) == expected

    def test_week_starts_on_monday(self):
        assert resolve_period("week", self.NOW) == (
            "2026W38", datetime(2026, 9, 14), datetime(2026, 9, 21),
        )

    def test_day(self):
        assert resolve_period("day", self.NOW) == (
            "20260918", datetime(2026, 9, 18), datetime(2026, 9, 19),
        )

    def test_explicit_partitions(self):
        assert resolve_period("202512", self.NOW) == (
            "202512", datetime(2025, 12, 1), datetime(2026, 1, 1),
        )
        assert resolve_period("20260101", self.NOW)[1:] == (
            datetime(2026, 1, 1), datetime(2026, 1, 2),
        )
        assert resolve_period("2026w02", self.NOW) == (
            "2026W02", datetime(2026, 1, 5), datetime(2026, 1, 12),
        )

    def test_malformed_partitions_raise(self):
        with pytest.raises(ValueError):
            resolve_period("20261399", self.NOW)
        with pytest.raises(ValueError):
            resolve_period("2026w99", self.NOW)


# ── the SQL, against a real engine ───────────────────────────────────────────

@pytest.fixture
def session():
    # One shared in-memory connection, usable from any thread: the route tests
    # reuse this fixture and the handler does its reads in asyncio.to_thread.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, hidden INTEGER NULL)",
            "CREATE TABLE players (player_id INTEGER PRIMARY KEY, player_name VARCHAR(20), "
            "user_id INTEGER NULL, hidden INTEGER NULL)",
            "CREATE TABLE user_group_association (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "player_id INTEGER NULL, user_id INTEGER NULL, group_id INTEGER NOT NULL)",
            "CREATE TABLE player_points (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "player_id INTEGER NOT NULL, group_id INTEGER NULL, amount INTEGER NOT NULL, "
            "reason VARCHAR(125), date_added DATETIME)",
            "CREATE TABLE group_configurations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id INTEGER NOT NULL, config_key VARCHAR(60), config_value VARCHAR(255))",
            "CREATE TABLE group_point_seasons (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id INTEGER NOT NULL, name VARCHAR(100), start_at DATETIME, end_at DATETIME)",
        ):
            conn.execute(text(ddl))
    with Session(engine) as s:
        yield s


def _player(s, pid, name, user_id=None, hidden=None):
    s.execute(
        text("INSERT INTO players (player_id, player_name, user_id, hidden) "
             "VALUES (:p, :n, :u, :h)"),
        {"p": pid, "n": name, "u": user_id, "h": hidden},
    )


def _user(s, uid, hidden=None):
    s.execute(text("INSERT INTO users (user_id, hidden) VALUES (:u, :h)"), {"u": uid, "h": hidden})


def _join(s, pid, group_id=GROUP):
    s.execute(
        text("INSERT INTO user_group_association (player_id, group_id) VALUES (:p, :g)"),
        {"p": pid, "g": group_id},
    )


def _leave(s, pid, group_id=GROUP):
    s.execute(
        text("DELETE FROM user_group_association WHERE player_id = :p AND group_id = :g"),
        {"p": pid, "g": group_id},
    )


def _award(s, pid, amount, when="2026-09-10 12:00:00", group_id=GROUP, reason="drop"):
    s.execute(
        text("INSERT INTO player_points (player_id, group_id, amount, reason, date_added) "
             "VALUES (:p, :g, :a, :r, :d)"),
        {"p": pid, "g": group_id, "a": amount, "r": reason, "d": when},
    )


def _set_combine(s, on=True, group_id=GROUP):
    s.execute(
        text("INSERT INTO group_configurations (group_id, config_key, config_value) "
             "VALUES (:g, :k, :v)"),
        {"g": group_id, "k": COMBINE_CONFIG_KEY, "v": "1" if on else "0"},
    )


@pytest.fixture
def clan(session):
    """User 9 owns Main + Alt; Solo is unclaimed. All three are members."""
    _user(session, 9)
    _player(session, 1, "Main", user_id=9)
    _player(session, 2, "Alt", user_id=9)
    _player(session, 3, "Solo")
    for pid in (1, 2, 3):
        _join(session, pid)
    _award(session, 1, 50)
    _award(session, 2, 80)
    _award(session, 3, 100)
    return session


class TestLeaversDoNotStand:
    def test_everyone_present_stands(self, clan):
        board = load_standings(clan, GROUP)
        assert [(s.primary.name, s.points) for s in board] == [
            ("Solo", 100), ("Alt", 80), ("Main", 50),
        ]

    def test_a_leaver_disappears_and_the_board_closes_up(self, clan):
        _leave(clan, 3)
        board = load_standings(clan, GROUP)
        assert [(s.rank, s.primary.name) for s in board] == [(1, "Alt"), (2, "Main")]

    def test_leaving_deletes_nothing_so_rejoining_restores_the_total(self, clan):
        _leave(clan, 3)
        assert find_standing(load_standings(clan, GROUP), player_id=3) is None
        _join(clan, 3)
        assert find_standing(load_standings(clan, GROUP), player_id=3).points == 100

    def test_membership_is_per_group(self, clan):
        # In another clan, but not this one: their points here do not stand.
        _leave(clan, 3)
        _join(clan, 3, group_id=OTHER_GROUP)
        assert find_standing(load_standings(clan, GROUP), player_id=3) is None

    def test_other_groups_points_never_leak_in(self, clan):
        _join(clan, 3, group_id=OTHER_GROUP)
        _award(clan, 3, 999, group_id=OTHER_GROUP)
        assert find_standing(load_standings(clan, GROUP), player_id=3).points == 100

    def test_duplicate_membership_rows_do_not_double_points(self, clan):
        # The association's unique key cannot stop these (NULL user_id never
        # collides), and a JOIN against it would count Solo twice.
        _join(clan, 3)
        assert find_standing(load_standings(clan, GROUP), player_id=3).points == 100

    def test_discord_user_rows_are_not_members(self, clan):
        clan.execute(text(
            "INSERT INTO user_group_association (user_id, group_id) VALUES (9, :g)"
        ), {"g": GROUP})
        assert member_player_ids(clan, GROUP) == {1, 2, 3}


class TestCombinedBoard:
    def test_off_by_default(self, clan):
        assert combine_enabled(clan, GROUP) is False

    def test_stored_zero_is_off(self, clan):
        _set_combine(clan, on=False)
        assert combine_enabled(clan, GROUP) is False

    def test_toggle_sums_the_users_accounts(self, clan):
        _set_combine(clan)
        board = load_standings(clan, GROUP)
        assert [(s.rank, s.primary.name, s.points) for s in board] == [
            (1, "Alt", 130), (2, "Solo", 100),
        ]

    def test_toggle_is_per_group(self, clan):
        _set_combine(clan, group_id=OTHER_GROUP)
        assert combine_enabled(clan, GROUP) is False

    def test_a_duplicate_row_reads_the_one_the_settings_page_writes(self, clan):
        """``group_configurations`` has no unique key and does hold duplicate
        (group_id, config_key) pairs. The settings route updates the row its
        unordered ``.first()`` returns -- the lowest id -- so that is the row
        that must be read back, or an admin ticks the box, sees it saved, and
        the boards go on ignoring it."""
        _set_combine(clan, on=True)   # lowest id: what the admin's save edits
        _set_combine(clan, on=False)  # stale duplicate written earlier by hand
        assert combine_enabled(clan, GROUP) is True
        assert len(load_standings(clan, GROUP)) == 2

    def test_the_alt_that_left_stops_counting_the_main_stays(self, clan):
        # The case the two rules exist to get right together.
        _set_combine(clan)
        _leave(clan, 2)
        board = load_standings(clan, GROUP)
        assert [(s.primary.name, s.points) for s in board] == [("Solo", 100), ("Main", 50)]
        assert not find_standing(board, player_id=1).combined

    def test_explicit_combine_overrides_the_setting(self, clan):
        assert len(load_standings(clan, GROUP, combine=True)) == 2
        _set_combine(clan)
        assert len(load_standings(clan, GROUP, combine=False)) == 3

    def test_hidden_flags_come_from_player_or_user(self, session):
        _user(session, 5, hidden=1)
        _player(session, 10, "ViaUser", user_id=5)
        _player(session, 11, "ViaPlayer", hidden=1)
        _player(session, 12, "Seen")
        for pid in (10, 11, 12):
            _join(session, pid)
            _award(session, pid, 10)
        rows = {r.name: r.hidden for r in load_member_totals(session, GROUP)}
        assert rows == {"ViaUser": True, "ViaPlayer": True, "Seen": False}


class TestWindows:
    def test_window_bounds_are_start_inclusive_end_exclusive(self, clan):
        _award(clan, 3, 7, when="2026-08-31 23:59:59")
        _award(clan, 3, 5, when="2026-10-01 00:00:00")
        _token, start, end = resolve_period("202609", datetime(2026, 9, 18))
        board = load_standings(clan, GROUP, start=start, end=end)
        assert find_standing(board, player_id=3).points == 100

    def test_all_time_counts_everything(self, clan):
        _award(clan, 3, 7, when="2025-01-01 00:00:00")
        assert find_standing(load_standings(clan, GROUP), player_id=3).points == 107

    def test_empty_window_is_an_empty_board(self, clan):
        assert load_standings(
            clan, GROUP, start=datetime(2030, 1, 1), end=datetime(2030, 2, 1)
        ) == []


class TestDisplayTotal:
    def test_per_rsn_total_is_the_players_own(self, clan):
        assert display_total(clan, GROUP, 1) == 50

    def test_combined_total_spans_the_users_in_group_accounts(self, clan):
        _set_combine(clan)
        assert display_total(clan, GROUP, 1) == 130
        assert display_total(clan, GROUP, 2) == 130
        assert counted_player_ids(clan, GROUP, 1) == [1, 2]

    def test_combined_total_skips_the_account_that_left(self, clan):
        _set_combine(clan)
        _leave(clan, 2)
        assert display_total(clan, GROUP, 1) == 50

    def test_the_asked_for_player_always_counts(self, clan):
        # A total printed at award time must include the award that caused it,
        # even if the roster read raced the join.
        _set_combine(clan)
        _leave(clan, 1)
        assert counted_player_ids(clan, GROUP, 1) == [1, 2]

    def test_unclaimed_player_on_a_combined_board(self, clan):
        _set_combine(clan)
        assert display_total(clan, GROUP, 3) == 100

    def test_unknown_player_is_zero(self, clan):
        assert display_total(clan, GROUP, 404) == 0


class TestSmallReads:
    def test_member_subset(self, clan):
        _leave(clan, 2)
        assert member_player_ids(clan, GROUP, [1, 2, 99]) == {1}
        assert member_player_ids(clan, GROUP, []) == set()

    def test_recent_awards_newest_first_and_group_scoped(self, clan):
        _award(clan, 1, 3, reason="pb")
        _award(clan, 1, 999, group_id=OTHER_GROUP)
        rows = recent_awards(clan, GROUP, [1, 2], limit=2)
        assert [(r["player_id"], r["amount"], r["reason"]) for r in rows] == [
            (1, 3, "pb"), (2, 80, "drop"),
        ]
        assert recent_awards(clan, GROUP, []) == []

    def test_seasons_newest_first(self, clan):
        for name, start, end in (
            ("Spring", "2026-03-01 00:00:00", "2026-04-01 00:00:00"),
            ("Autumn", "2026-09-01 00:00:00", "2026-10-01 00:00:00"),
        ):
            clan.execute(
                text("INSERT INTO group_point_seasons (group_id, name, start_at, end_at) "
                     "VALUES (:g, :n, :s, :e)"),
                {"g": GROUP, "n": name, "s": start, "e": end},
            )
        assert [s["name"] for s in load_seasons(clan, GROUP)] == ["Autumn", "Spring"]
