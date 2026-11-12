"""Shadow testing: the switches, the diff store, point-in-time reads and the
WOM-shaped rows built from our own stat store."""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import PlayerStatSnapshot, StatsShadowDiff
from utils import stat_snapshots as store
from utils import stats_shadow
from utils.xp_history import SKILLS

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


player_stats = _load("player_stats_under_test", "services/player_stats.py")
report = _load("stats_shadow_report_under_test", "scripts/stats_shadow_report.py")

T0 = datetime(2026, 11, 1, 0, 0)


@pytest.fixture
def s():
    engine = create_engine("sqlite://")
    for model in (PlayerStatSnapshot, StatsShadowDiff):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _plugin(s, pid, when, kc=None, **xp):
    full = {**{skill: 0 for skill in SKILLS}, "hitpoints": 1154, **xp}
    assert store.record_plugin_snapshot(s, pid, full, activities=(lambda: kc) if kc else None, now=when)


def _hiscores(s, pid, when, skills, kc=None):
    return store.record_hiscores_snapshot(s, pid, skills, kc or {}, {}, now=when)


@pytest.fixture(autouse=True)
def _no_stored_switch(monkeypatch):
    """Unit tests never read a real switch."""
    monkeypatch.setattr(stats_shadow, "_stored", lambda consumer: "")


class TestSwitches:
    def test_a_stored_switch_is_used_and_the_environment_wins(self, monkeypatch):
        monkeypatch.setattr(stats_shadow, "_stored", lambda consumer: "shadow")
        monkeypatch.delenv("STATS_SOURCE_EVENTS", raising=False)
        assert stats_shadow.source("events") == "shadow"
        monkeypatch.setenv("STATS_SOURCE_EVENTS", "wom")
        assert stats_shadow.source("events") == "wom"

    def test_a_stored_switch_that_is_not_a_mode_is_ignored(self, monkeypatch):
        monkeypatch.setattr(stats_shadow, "_stored", lambda consumer: "banana")
        monkeypatch.delenv("STATS_SOURCE_EVENTS", raising=False)
        assert stats_shadow.source("events") == "wom"

    def test_default_is_wom(self, monkeypatch):
        monkeypatch.delenv("STATS_SOURCE_EVENTS", raising=False)
        assert stats_shadow.source("events") == "wom"

    @pytest.mark.parametrize("raw,expected", [("shadow", "shadow"), ('"ours"', "ours"), (" Shadow ", "shadow"),
                                              ("yes", "wom"), ("", "wom")])
    def test_only_the_three_values_count(self, monkeypatch, raw, expected):
        monkeypatch.setenv("STATS_SOURCE_EHB_RATES", raw)
        assert stats_shadow.source("ehb_rates") == expected

    def test_each_consumer_has_its_own_switch(self, monkeypatch):
        monkeypatch.setenv("STATS_SOURCE_EVENTS", "shadow")
        monkeypatch.delenv("STATS_SOURCE_PLAYER_EHB", raising=False)
        assert stats_shadow.source("events") == "shadow" and stats_shadow.source("player_ehb") == "wom"

    def test_match_needs_both_numbers(self):
        assert stats_shadow.is_match("events", 5, 5) is True
        assert stats_shadow.is_match("events", 5, 6) is False
        assert stats_shadow.is_match("events", None, 6) is None
        assert stats_shadow.is_match("player_ehb", 10.004, 10.0) is True


class TestDiffStore:
    def test_one_row_per_subject_updated_in_place(self, s):
        first = [stats_shadow.entry("zulrah", 10, 8, player_id=7, our_source="plugin")]
        stats_shadow.record(s, "events", "event:1", first, now=T0)
        second = [stats_shadow.entry("zulrah", 12, 12, player_id=7, our_source="hiscores")]
        stats_shadow.record(s, "events", "event:1", second, now=T0 + timedelta(hours=1))
        (row,) = s.query(StatsShadowDiff).all()
        assert (row.wom_value, row.our_value, row.our_source) == (12, 12, "hiscores")
        assert row.observations == 2 and row.worst_delta == 2
        assert row.first_seen == T0 and row.last_seen == T0 + timedelta(hours=1)

    def test_subjects_are_kept_apart(self, s):
        entries = [stats_shadow.entry("zulrah", 1, 1, player_id=7), stats_shadow.entry("vorkath", 1, 1, player_id=7),
                   stats_shadow.entry("zulrah", 1, 1, player_id=8)]
        stats_shadow.record(s, "events", "event:1", entries, now=T0)
        stats_shadow.record(s, "events", "event:2", entries[:1], now=T0)
        stats_shadow.record(s, "ehb_rates", "event:1", entries[:1], now=T0)
        assert s.query(StatsShadowDiff).count() == 5

    def test_a_side_with_no_number_is_stored_as_none_not_zero(self, s):
        stats_shadow.record(s, "events", "event:1", [stats_shadow.entry("zulrah", 10, None, player_id=7)], now=T0)
        row = s.query(StatsShadowDiff).one()
        assert row.our_value is None and row.worst_delta is None

    def test_long_text_is_trimmed_to_fit(self, s):
        stats_shadow.record(s, "events", "c" * 200, [stats_shadow.entry("m" * 200, 1, 1, detail="d" * 900)], now=T0)
        row = s.query(StatsShadowDiff).one()
        assert (len(row.context), len(row.metric), len(row.detail)) == (64, 64, 255)

    def test_recording_never_raises_when_the_table_is_missing(self, monkeypatch):
        import db.models.base as base

        engine = create_engine("sqlite://")
        monkeypatch.setattr(base, "Session", sessionmaker(bind=engine))
        assert stats_shadow.record_safely("events", "event:1", [stats_shadow.entry("zulrah", 1, 1)]) == 0

    def test_rate_comparison_covers_bosses_only_one_side_prices(self):
        out = stats_shadow.compare_ehb_rates({"zulrah": 40, "old_boss": 5}, {"zulrah": 42, "new_boss": 9})
        assert [(e["metric"], e["wom_value"], e["our_value"]) for e in out] == [
            ("new_boss", None, 9), ("old_boss", 5, None), ("zulrah", 40, 42)]


class TestValuesAt:
    def test_plugin_xp_and_hiscores_bosses_are_merged(self, s):
        _hiscores(s, 7, T0, {"attack": 900, "magic": 500}, {"zulrah": 40, "vorkath": 9})
        _plugin(s, 7, T0 + timedelta(hours=2), kc={"zulrah": 44}, attack=1200, magic=500)
        got = store.values_at(s, 7, T0 + timedelta(hours=3))
        assert got["skills"]["attack"] == 1200 and got["activities"] == {"zulrah": 44, "vorkath": 9}
        assert got["source"] == "plugin" and got["taken_at"] == T0 + timedelta(hours=2)

    def test_only_rows_at_or_before_the_moment_count(self, s):
        _hiscores(s, 7, T0, {"attack": 900})
        _hiscores(s, 7, T0 + timedelta(days=2), {"attack": 5000})
        assert store.values_at(s, 7, T0 + timedelta(days=1))["skills"]["attack"] == 900
        assert store.values_at(s, 7, T0 - timedelta(days=1)) is None

    def test_flagged_rows_are_ignored(self, s):
        _hiscores(s, 7, T0, {"attack": 9000})
        assert _hiscores(s, 7, T0 + timedelta(hours=1), {"attack": 100}) == store.FLAGGED
        assert store.values_at(s, 7, T0 + timedelta(days=1))["skills"]["attack"] == 9000

    def test_hiscores_rows_carry_ehp_and_ehb(self, s):
        _hiscores(s, 7, T0, {"attack": 5_000_000, "slayer": 3_000_000, "hitpoints": 4_000_000,
                             "defence": 3_000_000}, {"zulrah": 400})
        row = s.query(PlayerStatSnapshot).one()
        assert row.ehp > 0 and row.ehb == pytest.approx(400 / 42, abs=3)


class TestGained:
    def test_gain_over_a_window(self, s):
        _hiscores(s, 7, T0 - timedelta(days=1), {"attack": 1000, "runecraft": 50}, {"zulrah": 10})
        _hiscores(s, 7, T0 + timedelta(days=3), {"attack": 4000, "runecraft": 80}, {"zulrah": 25, "vorkath": 3})
        got = player_stats.gained(s, 7, T0, T0 + timedelta(days=5))
        assert got["attack"]["gained"] == 3000 and got["zulrah"]["gained"] == 15
        assert (got["attack"]["start"], got["attack"]["end"]) == (1000, 4000)
        # Our "runecraft" is WOM's "runecrafting".
        assert got["runecrafting"]["gained"] == 30 and "runecraft" not in got
        # No baseline for a boss first seen mid-window: unknown, so left out.
        assert "vorkath" not in got
        assert got["attack"]["partial"] is False and got["attack"]["source"] == "hiscores"
        assert got["attack"]["age_seconds"] == 2 * 86400

    def test_history_starting_inside_the_window_is_partial(self, s):
        _hiscores(s, 7, T0 + timedelta(days=1), {"attack": 1000})
        _hiscores(s, 7, T0 + timedelta(days=3), {"attack": 4000})
        got = player_stats.gained(s, 7, T0, T0 + timedelta(days=5))
        assert got["attack"]["gained"] == 3000 and got["attack"]["partial"] is True

    def test_no_history_is_nothing_not_zeros(self, s):
        assert player_stats.gained(s, 7, T0, T0 + timedelta(days=5)) == {}

    def test_rows_take_the_shape_wom_answers_in(self, s):
        _hiscores(s, 7, T0 - timedelta(days=1), {"attack": 1000}, {"zulrah": 10})
        _hiscores(s, 7, T0 + timedelta(days=3), {"attack": 4000}, {"zulrah": 25})
        rows = player_stats.bulk_gained_rows(
            s, [{"player_id": 7, "name": "Some One", "wom_id": 99}, {"player_id": 8, "name": "No Data", "wom_id": 5}],
            T0, T0 + timedelta(days=5))
        (row,) = rows
        assert row["player"]["id"] == 99 and row["player"]["username"] == "Some One"
        assert row["player"]["updatedAt"] == "2026-11-04T00:00:00.000Z" == row["endDate"]
        assert {"metric": "zulrah", "start": 10, "end": 25, "gained": 15} in row["data"]


class TestEventComparison:
    def test_pairs_per_participant_and_metric(self):
        wom = {7: {"zulrah": {"start": 10, "end": 25, "gained": 15}, "attack": {"start": 0, "end": 900, "gained": None}},
               8: {"zulrah": {"start": 1, "end": 2, "gained": 1}}}
        ours = {7: {"zulrah": {"start": 10, "end": 24, "gained": 14, "partial": False, "source": "plugin",
                               "age_seconds": 60}}}
        out = stats_shadow.compare_gained_rows(wom, ours, ["zulrah", "attack"])
        by = {(e["player_id"], e["metric"]): e for e in out}
        assert (by[(7, "zulrah")]["wom_value"], by[(7, "zulrah")]["our_value"]) == (15, 14)
        assert by[(7, "zulrah")]["our_source"] == "plugin" and "ours 10->24" in by[(7, "zulrah")]["detail"]
        ours[7]["zulrah"]["partial"] = True
        late = stats_shadow.compare_gained_rows(wom, ours, ["zulrah"])
        assert late[0]["our_source"] == "plugin/partial"
        assert by[(7, "attack")]["our_value"] is None          # we have no reading
        assert by[(8, "zulrah")]["our_value"] is None          # a player we have no history for
        assert len(out) == 3


class TestReport:
    def _row(self, **over):
        base = dict(consumer="events", context="event:1", player_id=7, metric="zulrah", wom_value=10.0,
                    our_value=10.0, our_source="hiscores", last_seen=T0)
        base.update(over)
        return SimpleNamespace(**base)

    def test_counts_matches_gaps_and_players(self):
        rows = [self._row(), self._row(metric="vorkath", our_value=9.0),
                self._row(player_id=8, our_value=None), self._row(player_id=9, wom_value=None),
                self._row(consumer="player_ehb", context="type:main", wom_value=10.004, our_value=10.0)]
        out = report.summarise(rows, lambda c: stats_shadow.TOLERANCE.get(c, 0.0))
        events = out[("events", "event:1")]
        assert (events["rows"], events["compared"], events["matched"]) == (4, 2, 1)
        assert (events["no_data_ours"], events["no_data_wom"], len(events["players"])) == (1, 1, 3)
        assert out[("player_ehb", "type:main")]["matched"] == 1

    def test_a_late_start_is_counted_apart_from_real_differences(self):
        rows = [self._row(our_value=3.0, our_source="hiscores/partial"), self._row(metric="vorkath")]
        events = report.summarise(rows, lambda c: 0.0)[("events", "event:1")]
        assert (events["partial"], events["compared"], events["matched"]) == (1, 1, 1)
