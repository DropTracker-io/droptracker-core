"""Unit tests for the global loot-leader badge evaluator (services/badges.py).

Loaded from the file path (like test_event_lifecycle_sweep.py) so the conftest
``services`` stub doesn't shadow the real module; its own imports (``db``,
``utils.redis``) are the conftest MagicMocks, which is exactly what we want —
every DB/Redis touch here is a fake we control.
"""

import importlib.util
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "badges.py",
)
_spec = importlib.util.spec_from_file_location("_badges_under_test", _MODULE_PATH)
badges = importlib.util.module_from_spec(_spec)
sys.modules["_badges_under_test"] = badges
_spec.loader.exec_module(badges)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeQuery:
    """Chainable stand-in for a SQLAlchemy query returning fixed rows."""

    def __init__(self, rows):
        self._rows = rows

    def outerjoin(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.commits = 0

    def query(self, *cols):
        return FakeQuery(self.rows)

    def commit(self):
        self.commits += 1


class FakeRedis:
    """Records the key each zrevrange asked for and replays canned boards."""

    def __init__(self, boards):
        self.boards = boards
        self.keys_read = []
        self.ranges_read = []

    def zrevrange(self, key, start, end, withscores=False):
        self.keys_read.append(key)
        self.ranges_read.append((start, end))
        return self.boards.get(key, [])[start:end + 1]


def board(*pairs):
    """Redis returns members as bytes; scores as floats."""
    return [(str(pid).encode(), float(score)) for pid, score in pairs]


def make_badge(semantic="held", key="global_loot_leader_alltime", badge_id=6):
    return SimpleNamespace(badge_id=badge_id, key=key, semantic=semantic)


@pytest.fixture
def redis_boards(monkeypatch):
    """Install a fake Redis; tests fill ``.boards`` with the sets they need."""
    fake = FakeRedis({})
    monkeypatch.setattr(badges, "redis_client", SimpleNamespace(client=fake))
    return fake


@pytest.fixture
def no_writes(monkeypatch):
    """Record award/transfer calls instead of hitting the DB."""
    calls = {"transfer": [], "award": []}

    def _transfer(session, badge, slot_key, new_player_id, context=None, group_id=None):
        calls["transfer"].append((badge.key, slot_key, new_player_id, context))
        return calls.get("transfer_outcome", "awarded")

    def _award(session, badge, player_id, slot_key, context=None, **kw):
        calls["award"].append((badge.key, slot_key, player_id, context))
        return calls.get("award_result", object())

    monkeypatch.setattr(badges, "transfer_held_badge", _transfer)
    monkeypatch.setattr(badges, "award_badge", _award)
    return calls


@pytest.fixture
def everyone_visible(monkeypatch):
    monkeypatch.setattr(badges, "_visible_player_ids", lambda session, ids: set(ids))


# --------------------------------------------------------------------------- #
# Board reading / eligibility
# --------------------------------------------------------------------------- #
class TestLeaderSelection:
    def test_all_time_reads_the_all_board_and_takes_the_top(
            self, redis_boards, no_writes, everyone_visible):
        redis_boards.boards["leaderboard:all"] = board((738, 18107524630), (11, 11922185771))
        assert badges.evaluate_global_champion(FakeSession(), make_badge()) == 1
        assert redis_boards.keys_read == ["leaderboard:all"]
        assert no_writes["transfer"] == [
            ("global_loot_leader_alltime", "all",
             738, {"period": "all", "loot": 18107524630}),
        ]

    def test_month_period_reads_that_months_board(
            self, redis_boards, no_writes, everyone_visible):
        redis_boards.boards["leaderboard:202607"] = board((5752150, 4588325255))
        badge = make_badge(key="global_loot_leader_monthly", badge_id=7)
        assert badges.evaluate_global_champion(FakeSession(), badge, period="202607") == 1
        assert redis_boards.keys_read == ["leaderboard:202607"]
        assert no_writes["transfer"][0][1] == "202607"  # one slot per month

    def test_hidden_and_missing_players_are_skipped(self, redis_boards, no_writes, monkeypatch):
        redis_boards.boards["leaderboard:all"] = board((1, 900), (2, 800), (3, 700))
        # 1 is hidden, 2 has no players row -> the badge follows the site's #1.
        monkeypatch.setattr(badges, "_visible_player_ids", lambda session, ids: {3})
        assert badges.evaluate_global_champion(FakeSession(), make_badge()) == 1
        assert no_writes["transfer"][0][2] == 3
        assert no_writes["transfer"][0][3]["loot"] == 700

    def test_no_visible_player_awards_nothing(self, redis_boards, no_writes, monkeypatch):
        redis_boards.boards["leaderboard:all"] = board((1, 900))
        monkeypatch.setattr(badges, "_visible_player_ids", lambda session, ids: set())
        assert badges.evaluate_global_champion(FakeSession(), make_badge()) == 0
        assert no_writes["transfer"] == []

    def test_empty_board_awards_nothing(self, redis_boards, no_writes, everyone_visible):
        assert badges.evaluate_global_champion(FakeSession(), make_badge()) == 0
        assert no_writes["transfer"] == []

    def test_zero_and_negative_scores_are_not_a_lead(
            self, redis_boards, no_writes, everyone_visible):
        redis_boards.boards["leaderboard:all"] = board((1, 0), (2, -5))
        assert badges.evaluate_global_champion(FakeSession(), make_badge()) == 0
        assert no_writes["transfer"] == []

    def test_scan_depth_is_bounded(self, redis_boards, no_writes, everyone_visible):
        redis_boards.boards["leaderboard:all"] = board(*[(i, 1000 - i) for i in range(100)])
        badges.evaluate_global_champion(FakeSession(), make_badge())
        # Only the top slice is pulled back, never the whole board.
        assert redis_boards.ranges_read == [(0, badges._LEADER_SCAN - 1)]

    def test_str_members_decode_too(self, redis_boards, no_writes, everyone_visible):
        redis_boards.boards["leaderboard:all"] = [("42", 500.0)]
        assert badges.evaluate_global_champion(FakeSession(), make_badge()) == 1
        assert no_writes["transfer"][0][2] == 42


# --------------------------------------------------------------------------- #
# held vs permanent
# --------------------------------------------------------------------------- #
class TestSemantics:
    def test_held_retained_is_not_a_change(self, redis_boards, no_writes, everyone_visible):
        redis_boards.boards["leaderboard:all"] = board((738, 100))
        no_writes["transfer_outcome"] = "retained"
        session = FakeSession()
        assert badges.evaluate_global_champion(session, make_badge()) == 0
        assert session.commits == 1  # context refresh still gets committed

    def test_held_transfer_counts_as_a_change(self, redis_boards, no_writes, everyone_visible):
        redis_boards.boards["leaderboard:all"] = board((738, 100))
        no_writes["transfer_outcome"] = "transferred"
        assert badges.evaluate_global_champion(FakeSession(), make_badge()) == 1

    def test_permanent_skips_a_month_still_in_progress(
            self, redis_boards, no_writes, everyone_visible, monkeypatch):
        monkeypatch.setattr(badges, "_period_closed", lambda period, now=None: False)
        redis_boards.boards["leaderboard:202607"] = board((738, 100))
        badge = make_badge(semantic="permanent", key="global_loot_leader_monthly")
        assert badges.evaluate_global_champion(FakeSession(), badge, period="202607") == 0
        assert no_writes["award"] == [] and no_writes["transfer"] == []
        assert redis_boards.keys_read == []  # not even a board read

    def test_permanent_awards_a_finished_month(
            self, redis_boards, no_writes, everyone_visible, monkeypatch):
        monkeypatch.setattr(badges, "_period_closed", lambda period, now=None: True)
        redis_boards.boards["leaderboard:202606"] = board((5752128, 3472840603))
        badge = make_badge(semantic="permanent", key="global_loot_leader_monthly")
        assert badges.evaluate_global_champion(FakeSession(), badge, period="202606") == 1
        assert no_writes["award"] == [
            ("global_loot_leader_monthly", "202606",
             5752128, {"period": "202606", "loot": 3472840603}),
        ]
        assert no_writes["transfer"] == []

    def test_permanent_is_idempotent_once_the_slot_is_taken(
            self, redis_boards, no_writes, everyone_visible, monkeypatch):
        monkeypatch.setattr(badges, "_period_closed", lambda period, now=None: True)
        redis_boards.boards["leaderboard:202606"] = board((1, 100))
        no_writes["award_result"] = None  # award_badge refuses a taken slot
        badge = make_badge(semantic="permanent", key="global_loot_leader_monthly")
        session = FakeSession()
        assert badges.evaluate_global_champion(session, badge, period="202606") == 0
        assert session.commits == 0

    def test_dry_run_writes_nothing(self, redis_boards, no_writes, everyone_visible, monkeypatch):
        redis_boards.boards["leaderboard:all"] = board((738, 100))
        monkeypatch.setattr(badges, "_held_by", lambda *a, **k: None)
        session = FakeSession()
        assert badges.evaluate_global_champion(session, make_badge(), dry_run=True) == 1
        assert no_writes["transfer"] == [] and no_writes["award"] == []
        assert session.commits == 0

    def test_dry_run_reports_no_change_when_the_leader_already_holds_it(
            self, redis_boards, no_writes, everyone_visible, monkeypatch):
        redis_boards.boards["leaderboard:all"] = board((738, 100))
        monkeypatch.setattr(badges, "_held_by", lambda *a, **k: 738)
        assert badges.evaluate_global_champion(
            FakeSession(), make_badge(), dry_run=True) == 0


# --------------------------------------------------------------------------- #
# Privacy filter
# --------------------------------------------------------------------------- #
class TestVisiblePlayerIds:
    def test_null_flags_mean_visible_and_true_flags_hide(self):
        session = FakeSession(rows=[
            (1, None, None),   # no flags at all
            (2, True, None),   # player hidden
            (3, None, True),   # owning user hidden
            (4, 0, 0),         # explicit false
        ])
        assert badges._visible_player_ids(session, [1, 2, 3, 4]) == {1, 4}

    def test_ids_with_no_player_row_are_dropped(self):
        # 99 is in the sorted set but the query returns nothing for it.
        session = FakeSession(rows=[(1, None, None)])
        assert badges._visible_player_ids(session, [1, 99]) == {1}

    def test_empty_input_short_circuits(self):
        assert badges._visible_player_ids(None, []) == set()


# --------------------------------------------------------------------------- #
# Period plumbing
# --------------------------------------------------------------------------- #
class TestPeriods:
    def test_period_closed_only_for_past_months(self):
        now = datetime(2026, 7, 25)
        assert badges._period_closed("202606", now) is True
        assert badges._period_closed("202607", now) is False
        assert badges._period_closed("202608", now) is False
        assert badges._period_closed("all", now) is False

    def test_months_to_process_covers_the_month_that_just_ended(self):
        # First cycle of August, catching up July 31 -> both slots converge.
        assert badges._months_to_process(["20260731"], datetime(2026, 8, 1)) == \
            ["202608", "202607"]

    def test_months_to_process_is_just_the_current_month_mid_month(self):
        assert badges._months_to_process(["20260724"], datetime(2026, 7, 25)) == ["202607"]
        assert badges._months_to_process([], datetime(2026, 7, 25)) == ["202607"]

    def test_months_to_process_ignores_junk_day_tokens(self):
        assert badges._months_to_process(["nonsense"], datetime(2026, 7, 25)) == ["202607"]

    def test_leader_periods_from_criteria(self):
        months = ["202607", "202606"]
        assert badges._leader_periods({}, months) == ["all"]
        assert badges._leader_periods({"period": "all"}, months) == ["all"]
        assert badges._leader_periods({"period": "alltime"}, months) == ["all"]
        assert badges._leader_periods({"period": "month"}, months) == months
        assert badges._leader_periods({"period": "monthly"}, months) == months
        assert badges._leader_periods({"period": "202605"}, months) == ["202605"]
        assert badges._leader_periods({"period": "banana"}, months) == []
