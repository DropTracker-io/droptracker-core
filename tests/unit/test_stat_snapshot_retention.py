"""player_stat_snapshots retention: what survives, and that nothing is
deleted before its archive is proven."""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import NpcList, PlayerNpcKc, PlayerStatSnapshot
from utils import stat_snapshot_retention as retention
from utils.stat_snapshots import (
    decode_activities,
    encode_activities,
    plugin_boss_scores,
    record_plugin_snapshot,
)
from utils.xp_history import SKILLS

DAY = date(2026, 7, 1)
TODAY = date(2026, 11, 11)


@pytest.fixture
def s():
    engine = create_engine("sqlite://")
    for model in (PlayerStatSnapshot, NpcList, PlayerNpcKc):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add(s, player_id, hour, *, day=DAY, source="plugin", flagged=False, pinned=False, xp=1):
    s.add(PlayerStatSnapshot(
        player_id=player_id, taken_at=datetime(day.year, day.month, day.day, hour),
        source=source, skills='{"attack":%d}' % xp, total_xp=xp, total_level=1,
        flagged=flagged, pinned=pinned))
    s.commit()


def _left(s):
    return sorted((r.player_id, r.taken_at.day, r.taken_at.hour, r.source)
                  for r in s.query(PlayerStatSnapshot).all())


class Bucket:
    """Object storage stand-in."""

    def __init__(self):
        self.objects = {}
        self.fail_put = False
        self.corrupt = False

    def put(self, key, data):
        if self.fail_put:
            raise ConnectionError("b2 is down")
        self.objects[key] = retention.serialize_rows([]) if self.corrupt else data

    def get(self, key):
        return self.objects.get(key)


def _run(s, bucket, *, apply=True, mark=None, max_days=30, today=TODAY):
    marks = [mark] if mark else []
    report = retention.run(
        s, today, apply=apply, max_days=max_days, put=bucket.put, get=bucket.get,
        read_watermark=lambda: marks[-1] if marks else None,
        write_watermark=marks.append, log=lambda _line: None)
    return report, marks


class TestWhatSurvives:
    def _keys(self, rows):
        return sorted((k[0], k[1][11:13], k[2]) for k in retention.rows_to_delete(rows))

    def _row(self, player_id, hour, **over):
        return {"player_id": player_id, "taken_at": datetime(2026, 7, 1, hour), "source": "plugin",
                "flagged": False, "pinned": False, **over}

    def test_last_row_of_the_day_per_player_is_kept(self):
        rows = [self._row(1, 9), self._row(1, 14), self._row(1, 22), self._row(2, 3)]
        assert self._keys(rows) == [(1, "09", "plugin"), (1, "14", "plugin")]

    def test_pinned_rows_are_kept_as_well(self):
        rows = [self._row(1, 9, pinned=True), self._row(1, 14), self._row(1, 22)]
        assert self._keys(rows) == [(1, "14", "plugin")]

    def test_a_flagged_row_is_never_the_one_kept(self):
        rows = [self._row(1, 9), self._row(1, 22, flagged=True)]
        assert self._keys(rows) == [(1, "22", "plugin")]

    def test_the_last_row_wins_across_sources(self):
        rows = [self._row(1, 9), self._row(1, 20, source="hiscores")]
        assert self._keys(rows) == [(1, "09", "plugin")]

    def test_a_single_row_day_loses_nothing(self):
        assert self._keys([self._row(1, 9)]) == []


class TestDownsample:
    def test_old_day_is_archived_in_full_then_thinned(self, s):
        for hour in (9, 14, 22):
            _add(s, 1, hour, xp=hour)
        _add(s, 2, 5)
        bucket = Bucket()
        report, marks = _run(s, bucket, max_days=1)
        assert _left(s) == [(1, 1, 22, "plugin"), (2, 1, 5, "plugin")]
        archived = retention.parse_archive(bucket.objects[retention.archive_key(DAY)])
        assert len(archived) == 4 and {r["total_xp"] for r in archived} == {1, 9, 14, 22}
        assert report["days"]["2026-07-01"] == {"rows": 4, "doomed": 2, "archived": 4, "deleted": 2}
        assert marks == ["2026-07-01"]

    def test_export_failure_blocks_deletion(self, s):
        _add(s, 1, 9)
        _add(s, 1, 22)
        bucket = Bucket()
        bucket.fail_put = True
        with pytest.raises(retention.ArchiveError):
            _run(s, bucket)
        assert len(_left(s)) == 2

    def test_an_archive_that_does_not_read_back_whole_blocks_deletion(self, s):
        _add(s, 1, 9)
        _add(s, 1, 22)
        bucket = Bucket()
        bucket.corrupt = True
        with pytest.raises(retention.ArchiveError):
            _run(s, bucket)
        assert len(_left(s)) == 2

    def test_a_failed_day_is_not_marked_done(self, s):
        _add(s, 1, 9)
        _add(s, 1, 22)
        bucket = Bucket()
        bucket.fail_put = True
        marks = []
        with pytest.raises(retention.ArchiveError):
            retention.run(s, TODAY, apply=True, max_days=5, put=bucket.put, get=bucket.get,
                          read_watermark=lambda: None, write_watermark=marks.append,
                          log=lambda _line: None)
        assert marks == []

    def test_a_rerun_merges_with_the_archive_it_finds(self, s):
        for hour in (9, 14, 22):
            _add(s, 1, hour)
        bucket = Bucket()
        _run(s, bucket, max_days=1)
        # A late row for the same day arrives; the day is processed again.
        _add(s, 1, 23)
        _run(s, bucket, max_days=1)
        archived = retention.parse_archive(bucket.objects[retention.archive_key(DAY)])
        assert sorted(r["taken_at"][11:13] for r in archived) == ["09", "14", "22", "23"]
        assert _left(s) == [(1, 1, 23, "plugin")]

    def test_days_inside_the_hot_window_are_left_whole(self, s):
        recent = date(2026, 9, 1)
        _add(s, 1, 9, day=recent)
        _add(s, 1, 22, day=recent)
        bucket = Bucket()
        report, _ = _run(s, bucket)
        assert len(_left(s)) == 2 and bucket.objects == {} and report["days"] == {}

    def test_the_hot_window_edge(self, s):
        # 90 days before today is still hot; 91 is not.
        for d in (date(2026, 8, 12), date(2026, 8, 13)):
            _add(s, 1, 9, day=d)
            _add(s, 1, 22, day=d)
        _run(s, Bucket())
        assert _left(s) == [(1, 12, 22, "plugin"), (1, 13, 9, "plugin"), (1, 13, 22, "plugin")]

    def test_a_dry_run_writes_nothing(self, s):
        _add(s, 1, 9)
        _add(s, 1, 22)
        bucket = Bucket()
        report, marks = _run(s, bucket, apply=False, max_days=1)
        assert len(_left(s)) == 2 and bucket.objects == {} and marks == []
        assert report["days"]["2026-07-01"]["doomed"] == 1

    def test_it_resumes_after_the_watermark(self, s):
        _add(s, 1, 9)
        _add(s, 1, 22)
        report, _ = _run(s, Bucket(), mark="2026-07-01", max_days=2)
        assert list(report["days"]) == ["2026-07-02", "2026-07-03"] and len(_left(s)) == 2

    def test_pinned_rows_survive_the_delete(self, s):
        _add(s, 1, 9, pinned=True)
        _add(s, 1, 14)
        _add(s, 1, 22)
        _run(s, Bucket(), max_days=1)
        assert _left(s) == [(1, 1, 9, "plugin"), (1, 1, 22, "plugin")]


class TestArchiveName:
    def test_a_keyed_name_is_stable_and_not_the_bare_date(self):
        keyed = retention.archive_key(DAY, "s3cret")
        assert keyed == retention.archive_key(DAY, "s3cret")
        assert keyed != retention.archive_key(DAY, "other") != retention.archive_key(DAY)
        assert keyed.startswith("dt_stats/snapshots/2026/07/2026-07-01-") and keyed.endswith(".jsonl.gz")

    def test_the_run_archives_under_the_keyed_name(self, s):
        _add(s, 1, 9)
        _add(s, 1, 22)
        bucket = Bucket()
        retention.run(s, TODAY, apply=True, max_days=1, put=bucket.put, get=bucket.get,
                      read_watermark=lambda: None, write_watermark=lambda _d: None,
                      log=lambda _line: None, secret="s3cret")
        assert list(bucket.objects) == [retention.archive_key(DAY, "s3cret")]


class TestPartitions:
    def test_months_are_added_up_to_six_ahead(self):
        existing = ["p202611", "p202612", "pmax"]
        out = retention.missing_partitions(existing, date(2026, 11, 11))
        assert [name for name, _ in out] == ["p202701", "p202702", "p202703", "p202704", "p202705"]
        assert out[0][1] == "2027-02-01 00:00:00"

    def test_nothing_to_add_when_far_enough_ahead(self):
        existing = [f"p2027{m:02d}" for m in range(1, 13)] + ["pmax"]
        assert retention.missing_partitions(existing, date(2026, 11, 11)) == []

    def test_a_gap_after_a_long_outage_is_filled_in_order(self):
        out = retention.missing_partitions(["p202601", "pmax"], date(2026, 3, 5), ahead=1)
        assert [name for name, _ in out] == ["p202602", "p202603", "p202604"]

    def test_an_unpartitioned_table_is_left_alone(self):
        assert retention.missing_partitions([], date(2026, 11, 11)) == []
        assert retention.missing_partitions(["p202611"], date(2026, 11, 11)) == []


class TestBossScores:
    def _metric(self, name):
        return {"Zulrah": "zulrah", "Vorkath": "vorkath", "Vorkath (quest)": "vorkath"}.get(name)

    def _seed(self, s):
        for npc_id, name, kc in ((1, "Zulrah", 40), (2, "Vorkath", 7), (3, "Vorkath (quest)", 1), (4, "Goblin", 900)):
            s.add(NpcList(npc_id=npc_id, npc_name=name))
            s.add(PlayerNpcKc(player_id=7, npc_id=npc_id, kill_count=kc))
        s.commit()

    def test_only_hiscores_bosses_and_the_higher_count_per_metric(self, s):
        self._seed(s)
        assert plugin_boss_scores(s, 7, self._metric) == {"zulrah": 40, "vorkath": 7}
        assert plugin_boss_scores(s, 8, self._metric) == {}

    def test_scores_ride_on_the_snapshot(self, s):
        self._seed(s)
        xp = {**{skill: 0 for skill in SKILLS}, "hitpoints": 1154}
        record_plugin_snapshot(s, 7, xp, activities=lambda: plugin_boss_scores(s, 7, self._metric),
                               now=datetime(2026, 11, 11, 12))
        (row,) = s.query(PlayerStatSnapshot).all()
        assert decode_activities(row.activities) == {"vorkath": 7, "zulrah": 40}

    def test_a_failing_score_lookup_still_writes_the_xp(self, s):
        xp = {**{skill: 0 for skill in SKILLS}, "hitpoints": 1154}

        def boom():
            raise RuntimeError("no npc table")

        assert record_plugin_snapshot(s, 7, xp, activities=boom, now=datetime(2026, 11, 11, 12))
        assert s.query(PlayerStatSnapshot).one().activities is None

    def test_zero_and_junk_scores_are_unknown_not_zero(self):
        assert encode_activities({"zulrah": 0, "vorkath": 3, "x": True, "y": "9"}) == '{"vorkath":3}'
        assert encode_activities({}) is None
        assert decode_activities("not json") == {}
