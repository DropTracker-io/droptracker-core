"""player_stat_snapshots: the one writer, and the baseline lookups."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import PlayerStatSnapshot
from utils.stat_snapshots import (
    SOURCE_HISCORES,
    SOURCE_PLUGIN,
    decode_skills,
    earliest_snapshot,
    record_plugin_snapshot,
    same_bucket,
    snapshot_at,
    xp_baselines,
)
from utils.xp_history import SKILLS, gains_since

PID = 7


@pytest.fixture
def s():
    engine = create_engine("sqlite://")
    PlayerStatSnapshot.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _rows(s):
    return s.query(PlayerStatSnapshot).order_by(PlayerStatSnapshot.taken_at).all()


def _xp(**over):
    return {**{skill: 0 for skill in SKILLS}, "hitpoints": 1154, **over}


class TestWriter:
    def test_first_sync_writes_a_row(self, s):
        assert record_plugin_snapshot(s, PID, _xp(attack=100), account_type=1,
                                      now=datetime(2026, 11, 10, 12, 0))
        (row,) = _rows(s)
        assert (row.source, row.account_type, row.total_xp) == (SOURCE_PLUGIN, 1, 1254)
        assert decode_skills(row.skills)["attack"] == 100
        assert row.total_level == len(SKILLS) - 1 + 10 + 1  # attack 100 xp is level 2

    def test_unchanged_xp_writes_nothing_however_long_after(self, s):
        record_plugin_snapshot(s, PID, _xp(attack=100), now=datetime(2026, 11, 10, 12, 0))
        assert not record_plugin_snapshot(s, PID, _xp(attack=100), now=datetime(2026, 11, 12, 9, 0))
        assert len(_rows(s)) == 1

    def test_syncs_in_one_window_share_a_row_holding_the_closing_values(self, s):
        record_plugin_snapshot(s, PID, _xp(attack=100), now=datetime(2026, 11, 10, 12, 1))
        assert record_plugin_snapshot(s, PID, _xp(attack=150), now=datetime(2026, 11, 10, 12, 14))
        (row,) = _rows(s)
        assert row.taken_at == datetime(2026, 11, 10, 12, 14)
        assert decode_skills(row.skills)["attack"] == 150

    def test_a_new_window_gets_a_new_row(self, s):
        record_plugin_snapshot(s, PID, _xp(attack=100), now=datetime(2026, 11, 10, 12, 14))
        record_plugin_snapshot(s, PID, _xp(attack=150), now=datetime(2026, 11, 10, 12, 15))
        assert [decode_skills(r.skills)["attack"] for r in _rows(s)] == [100, 150]

    def test_a_pinned_row_is_never_overwritten(self, s):
        record_plugin_snapshot(s, PID, _xp(attack=100), now=datetime(2026, 11, 10, 12, 1))
        _rows(s)[0].pinned = True
        s.flush()
        record_plugin_snapshot(s, PID, _xp(attack=150), now=datetime(2026, 11, 10, 12, 5))
        assert [(r.pinned, decode_skills(r.skills)["attack"]) for r in _rows(s)] == [(True, 100), (False, 150)]

    def test_a_row_that_never_carried_xp_is_not_history(self, s):
        assert not record_plugin_snapshot(s, PID, {skill: 0 for skill in SKILLS})
        assert _rows(s) == []

    def test_a_clock_stepping_back_writes_nothing(self, s):
        record_plugin_snapshot(s, PID, _xp(attack=100), now=datetime(2026, 11, 10, 12, 30))
        assert not record_plugin_snapshot(s, PID, _xp(attack=150), now=datetime(2026, 11, 10, 12, 0))
        assert len(_rows(s)) == 1

    def test_players_do_not_share_history(self, s):
        record_plugin_snapshot(s, PID, _xp(attack=100), now=datetime(2026, 11, 10, 12, 0))
        assert record_plugin_snapshot(s, PID + 1, _xp(attack=100), now=datetime(2026, 11, 10, 12, 0))

    def test_a_missing_table_costs_the_caller_nothing(self):
        engine = create_engine("sqlite://")
        session = sessionmaker(bind=engine)()
        assert record_plugin_snapshot(session, PID, _xp(attack=1)) is False
        # The surrounding transaction is still usable.
        assert session.execute(__import__("sqlalchemy").text("SELECT 1")).scalar() == 1

    def test_windows_are_wall_clock_quarters(self):
        assert same_bucket(datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 1, 0, 14, 59))
        assert not same_bucket(datetime(2026, 1, 1, 0, 14), datetime(2026, 1, 1, 0, 15))
        assert not same_bucket(datetime(2026, 1, 1, 0, 5), datetime(2026, 1, 2, 0, 5))


class TestReaders:
    def _seed(self, s):
        for when, attack in ((datetime(2026, 10, 1, 9), 100),
                             (datetime(2026, 11, 2, 9), 300),
                             (datetime(2026, 11, 9, 11), 450)):
            record_plugin_snapshot(s, PID, _xp(attack=attack), now=when)

    def test_snapshot_at_is_the_latest_row_at_or_before(self, s):
        self._seed(s)
        assert snapshot_at(s, PID, datetime(2026, 9, 1)) is None
        assert snapshot_at(s, PID, datetime(2026, 11, 2, 9)).total_xp == 1154 + 300
        assert snapshot_at(s, PID, datetime(2026, 11, 5)).total_xp == 1154 + 300

    def test_flagged_rows_are_never_a_baseline(self, s):
        self._seed(s)
        s.add(PlayerStatSnapshot(player_id=PID, taken_at=datetime(2026, 9, 1), source=SOURCE_HISCORES,
                                 skills="{}", flagged=True))
        s.flush()
        assert earliest_snapshot(s, PID).taken_at == datetime(2026, 10, 1, 9)
        assert snapshot_at(s, PID, datetime(2026, 9, 15)) is None

    def test_any_source_can_be_the_baseline(self, s):
        self._seed(s)
        s.add(PlayerStatSnapshot(player_id=PID, taken_at=datetime(2026, 11, 3), source=SOURCE_HISCORES,
                                 skills='{"attack":320,"hitpoints":1154}'))
        s.flush()
        assert snapshot_at(s, PID, datetime(2026, 11, 3, 12)).source == SOURCE_HISCORES

    def test_profile_gains_end_to_end(self, s):
        self._seed(s)
        now = datetime(2026, 11, 10, 12)
        baselines, earliest = xp_baselines(s, PID, now)
        out = gains_since(_xp(attack=500), baselines, earliest, now)
        assert out["day"]["skills"] == {"attack": 50}
        assert out["week"]["skills"] == {"attack": 200}
        assert out["month"]["skills"] == {"attack": 400}
        assert not any(out[p]["partial"] for p in out)

    def test_history_shorter_than_the_period_is_partial(self, s):
        record_plugin_snapshot(s, PID, _xp(attack=400), now=datetime(2026, 11, 8, 20))
        now = datetime(2026, 11, 10, 12)
        baselines, earliest = xp_baselines(s, PID, now)
        out = gains_since(_xp(attack=500), baselines, earliest, now)
        assert out["week"]["partial"] and out["week"]["skills"] == {"attack": 100}
        assert out["day"]["partial"] is False

    def test_no_rows_no_gains(self, s):
        now = datetime(2026, 11, 10, 12)
        baselines, earliest = xp_baselines(s, PID, now)
        assert gains_since(_xp(), baselines, earliest, now) == {"day": None, "week": None, "month": None}
