"""The rule for which reading of a player's combat achievement total is kept.

Two writers feed ``player_ca_varps.points``: every completion (the game's own
total) and every account sync (what the synced bits are worth). The rule lives
in one conditional UPDATE, so it is exercised here against a real database
engine — SQLite — rather than by asserting on SQL text: the statement's
behaviour is the thing that matters, and MySQL's left-to-right SET evaluation
is exactly the kind of detail a string comparison would never catch.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# By name, never ``from db import ca_points``: conftest stubs the ``db``
# package with a MagicMock, whose attribute lookup would hand back a mock
# instead of the real module and let every assertion pass vacuously.
from db.ca_points import (
    MAX_POINTS,
    SOURCE_GAME,
    SOURCE_SYNC,
    _insert_missing_sql,
    _update_sql,
    load_task_registry,
    record_ca_points,
    reset_registry_cache,
    valid_points,
)

T0 = datetime(2026, 9, 10, 12, 0, 0)
PLAYER = 4137


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE player_ca_varps (
                player_id INTEGER PRIMARY KEY,
                varps TEXT NULL,
                tasks_completed INTEGER NULL,
                completed_tasks TEXT NULL,
                points INTEGER NULL,
                points_source VARCHAR(16) NULL,
                points_observed_at DATETIME NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text(
            "CREATE TABLE plugin_manifest_sections (`key` VARCHAR(64) PRIMARY KEY, payload TEXT)"
        ))
    with Session(engine) as s:
        yield s


def _row(session, player_id=PLAYER):
    row = session.execute(text(
        "SELECT points, points_source, points_observed_at, updated_at, varps "
        "FROM player_ca_varps WHERE player_id = :p"
    ), {"p": player_id}).first()
    if row is None:
        return None
    return {
        "points": row[0],
        "source": row[1],
        "observed_at": _dt(row[2]),
        "updated_at": _dt(row[3]),
        "varps": row[4],
    }


def _dt(value):
    # SQLite hands a bound datetime back as its ISO string.
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _record(session, points, source, observed_at, **kw):
    kw.setdefault("now", observed_at)
    return record_ca_points(session, PLAYER, points, source, observed_at, **kw)


class TestFirstReading:
    def test_creates_the_row_with_no_bits(self, session):
        # A player who completes a task before ever syncing still gets a
        # total. varps stays NULL: "no bits", which the site must not decode
        # as "every task undone".
        assert _record(session, 656, SOURCE_GAME, T0) is True
        assert _row(session) == {
            "points": 656, "source": "game", "observed_at": T0,
            "updated_at": T0, "varps": None,
        }

    def test_fills_in_an_existing_synced_row_without_touching_its_bits(self, session):
        session.execute(text(
            "INSERT INTO player_ca_varps (player_id, varps, tasks_completed, updated_at) "
            "VALUES (:p, :v, 3, :t)"
        ), {"p": PLAYER, "v": '{"3116":7}', "t": T0})
        assert _record(session, 6, SOURCE_SYNC, T0 + timedelta(hours=1)) is True
        row = _row(session)
        assert row["points"] == 6 and row["source"] == "sync"
        assert row["varps"] == '{"3116":7}'

    def test_a_zero_count_is_a_real_total_for_an_account_with_nothing_done(self, session):
        assert _record(session, 0, SOURCE_SYNC, T0) is True
        assert _row(session)["points"] == 0


class TestPointsNeverGoDown:
    def test_a_higher_reading_wins_even_when_it_is_older(self, session):
        # A sync counted 515 at 13:00 with a registry that has not caught up
        # with a new batch; the game said 520 at 12:55 and arrived late.
        # Points only rise, so 520 proves 515 is short.
        _record(session, 515, SOURCE_SYNC, T0 + timedelta(hours=1))
        assert _record(session, 520, SOURCE_GAME, T0 + timedelta(minutes=55)) is True
        assert _row(session)["points"] == 520

    def test_a_sync_count_never_lowers_a_total(self, session):
        # Seen in production: a login sync read before the game had sent the
        # varps, all zero, for players with completed tasks on record.
        _record(session, 780, SOURCE_GAME, T0)
        assert _record(session, 0, SOURCE_SYNC, T0 + timedelta(days=1)) is False
        assert _row(session)["points"] == 780

    def test_a_sync_count_never_lowers_an_earlier_sync_count_either(self, session):
        _record(session, 780, SOURCE_SYNC, T0)
        assert _record(session, 770, SOURCE_SYNC, T0 + timedelta(days=1)) is False
        assert _row(session)["points"] == 780

    def test_a_newer_in_game_total_may_lower_it(self, session):
        # The one way points really fall: Jagex moves a task to a cheaper
        # tier. The game's own number, taken later, is the truth.
        _record(session, 520, SOURCE_SYNC, T0)
        assert _record(session, 515, SOURCE_GAME, T0 + timedelta(minutes=5)) is True
        assert _row(session)["points"] == 515
        assert _row(session)["source"] == "game"

    def test_an_older_in_game_total_arriving_late_cannot_lower_it(self, session):
        # A queue backlog delivers a completion after a newer sync.
        _record(session, 782, SOURCE_SYNC, T0 + timedelta(minutes=30))
        assert _record(session, 780, SOURCE_GAME, T0) is False
        assert _row(session)["points"] == 782

    def test_a_non_authoritative_game_total_may_raise_but_not_lower(self, session):
        # The backfill's totals from notification history carry processing
        # times, not read times, so they are not trusted to lower anything.
        _record(session, 520, SOURCE_SYNC, T0)
        later = T0 + timedelta(days=1)
        assert _record(session, 510, SOURCE_GAME, later, authoritative=False) is False
        assert _record(session, 530, SOURCE_GAME, later, authoritative=False) is True
        assert _row(session)["points"] == 530


class TestTimestamps:
    def test_an_equal_reading_only_moves_the_observation_forward(self, session):
        _record(session, 520, SOURCE_GAME, T0)
        later = T0 + timedelta(hours=1)
        assert _record(session, 520, SOURCE_SYNC, later, now=later) is True
        row = _row(session)
        assert row["observed_at"] == later
        # Nothing about the player's progress changed.
        assert row["updated_at"] == T0

    def test_a_confirmed_total_blocks_an_older_lower_game_reading(self, session):
        # The reason equal readings move the time: 520 was confirmed at
        # 13:00, so a 518 read at 12:30 is stale, not a re-tier.
        _record(session, 520, SOURCE_GAME, T0)
        _record(session, 520, SOURCE_SYNC, T0 + timedelta(hours=1))
        assert _record(session, 518, SOURCE_GAME, T0 + timedelta(minutes=30)) is False
        assert _row(session)["points"] == 520

    def test_an_older_equal_reading_changes_nothing(self, session):
        _record(session, 520, SOURCE_GAME, T0 + timedelta(hours=1))
        assert _record(session, 520, SOURCE_SYNC, T0) is False
        assert _row(session)["observed_at"] == T0 + timedelta(hours=1)

    def test_a_higher_but_older_reading_keeps_the_newer_observation_time(self, session):
        _record(session, 515, SOURCE_SYNC, T0 + timedelta(hours=1))
        _record(session, 520, SOURCE_GAME, T0)
        assert _row(session)["observed_at"] == T0 + timedelta(hours=1)

    def test_updated_at_moves_when_the_total_does(self, session):
        _record(session, 500, SOURCE_GAME, T0)
        later = T0 + timedelta(hours=2)
        _record(session, 503, SOURCE_GAME, later, now=later)
        assert _row(session)["updated_at"] == later


class TestRejectedReadings:
    @pytest.mark.parametrize("bad", [None, -1, True, MAX_POINTS + 1, "abc", 2.5, ""])
    def test_an_unusable_total_is_ignored_and_creates_nothing(self, session, bad):
        assert _record(session, bad, SOURCE_GAME, T0) is False
        assert _row(session) is None

    def test_a_numeric_string_is_accepted(self, session):
        # The Discord webhook transport delivers embed fields as text.
        assert _record(session, "656", SOURCE_GAME, T0) is True
        assert _row(session)["points"] == 656

    def test_an_unknown_source_is_a_programming_error(self, session):
        with pytest.raises(ValueError):
            _record(session, 10, "wiki", T0)

    def test_no_observation_time_is_ignored(self, session):
        assert record_ca_points(session, PLAYER, 10, SOURCE_GAME, None) is False


class TestValidPoints:
    @pytest.mark.parametrize("value,expected", [
        (0, 0), (656, 656), ("656", 656), (" 12 ", 12), (MAX_POINTS, MAX_POINTS),
        (MAX_POINTS + 1, None), (-1, None), (True, None), (False, None),
        (None, None), (2.0, None), ("x", None),
    ])
    def test_valid_points(self, value, expected):
        assert valid_points(value) == expected


class TestMysqlStatement:
    def test_the_missing_row_insert_does_not_swallow_errors(self):
        # INSERT IGNORE would turn a foreign-key failure into a warning.
        sql = _insert_missing_sql("mysql")
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "IGNORE" not in sql.upper()

    def test_updated_at_is_assigned_before_points(self):
        # MySQL evaluates SET left to right against already-assigned values;
        # updated_at must compare against the *old* points.
        sql = _update_sql(True)
        assert sql.index("updated_at = CASE") < sql.index("points = :points")

    def test_only_an_authoritative_reading_can_lower(self):
        assert ":points < points" in _update_sql(True)
        assert ":points < points" not in _update_sql(False)


class TestTaskRegistry:
    @pytest.fixture(autouse=True)
    def _fresh(self):
        reset_registry_cache()
        yield
        reset_registry_cache()

    def _store(self, session, payload):
        session.execute(text("DELETE FROM plugin_manifest_sections"))
        session.execute(
            text("INSERT INTO plugin_manifest_sections (`key`, payload) VALUES (:k, :p)"),
            {"k": "combat_achievement_tasks", "p": payload},
        )

    def test_reads_the_tasks(self, session):
        tasks = [{"varp": 3116, "bit": 0, "tier": "Easy"}]
        self._store(session, json.dumps({"tasks": tasks}))
        assert load_task_registry(session) == tasks

    def test_an_empty_registry_is_never_cached(self, session):
        # The web API once cached an empty structure for the life of the
        # process and rendered "nothing recorded" over real data.
        self._store(session, json.dumps({}))
        assert load_task_registry(session) == []
        tasks = [{"varp": 3116, "bit": 0, "tier": "Easy"}]
        self._store(session, json.dumps({"tasks": tasks}))
        assert load_task_registry(session) == tasks

    def test_a_populated_registry_is_cached(self, session):
        first = [{"varp": 3116, "bit": 0, "tier": "Easy"}]
        self._store(session, json.dumps({"tasks": first}))
        assert load_task_registry(session) == first
        self._store(session, json.dumps({"tasks": []}))
        assert load_task_registry(session) == first

    @pytest.mark.parametrize("payload", ["not json", json.dumps([1, 2]), json.dumps({"tasks": "x"})])
    def test_an_unreadable_registry_is_empty(self, session, payload):
        self._store(session, payload)
        assert load_task_registry(session) == []

    def test_no_row_is_empty(self, session):
        assert load_task_registry(session) == []
