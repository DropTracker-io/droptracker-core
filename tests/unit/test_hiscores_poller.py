"""Official hiscores: parsing, the request gate, who is due, what gets stored."""
import asyncio
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import PlayerStatSnapshot
from utils import hiscores_schedule as schedule
from utils import osrs_hiscores as hiscores
from utils import stat_snapshots as store
from utils.xp_history import SKILLS

_spec = importlib.util.spec_from_file_location(
    "hiscores_poller_under_test",
    Path(__file__).resolve().parents[2] / "services" / "hiscores_poller.py")
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

NOW = datetime(2026, 11, 12, 12, 0)


def _payload(**over):
    skills = [{"id": 0, "name": "Overall", "rank": 5, "level": 50, "xp": 3000},
              {"id": 1, "name": "Attack", "rank": 10, "level": 20, "xp": 1000},
              {"id": 2, "name": "Runecraft", "rank": -1, "level": 1, "xp": -1},
              {"id": 3, "name": "Magic", "rank": 7, "level": 30, "xp": 2000}]
    activities = [{"id": 0, "name": "League Points", "rank": -1, "score": -1},
                  {"id": 1, "name": "Clue Scrolls (all)", "rank": 40, "score": 12},
                  {"id": 2, "name": "Theatre of Blood: Hard Mode", "rank": 3, "score": 9},
                  {"id": 3, "name": "Tombs of Amascut: Expert Mode", "rank": -1, "score": 0},
                  {"id": 4, "name": "K'ril Tsutsaroth", "rank": 8, "score": 55}]
    return {"name": "x", "skills": skills, "activities": activities, **over}


class FakeRedis:
    def __init__(self):
        self.data, self.hashes, self.broken = {}, {}, False

    def _check(self):
        if self.broken:
            raise ConnectionError("redis down")

    def get(self, key):
        self._check()
        return self.data.get(key)

    def set(self, key, value, nx=False, px=None, ex=None):
        self._check()
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def exists(self, key):
        self._check()
        return int(key in self.data)

    def incr(self, key):
        self._check()
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    def expire(self, key, ttl):
        return True

    def delete(self, key):
        self.data.pop(key, None)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        target = self.hashes.setdefault(key, {})
        if mapping:
            target.update({k: str(v) for k, v in mapping.items()})
        else:
            target[field] = str(value)

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)


class TestParse:
    def test_unranked_entries_are_left_out_not_zeroed(self):
        h = hiscores.parse(_payload())
        assert h.skills == {"attack": 1000, "magic": 2000}
        assert (h.total_xp, h.total_level) == (3000, 50)
        assert h.activities == {"clue_scrolls_all": 12, "theatre_of_blood_hard_mode": 9,
                                "kril_tsutsaroth": 55}
        assert h.ranks == {"overall": 5, "attack": 10, "magic": 7, "clue_scrolls_all": 40,
                           "theatre_of_blood_hard_mode": 3, "kril_tsutsaroth": 8}

    def test_entries_are_read_by_name_not_position(self):
        shuffled = _payload()
        shuffled["skills"].reverse()
        assert hiscores.parse(shuffled).skills == {"attack": 1000, "magic": 2000}

    @pytest.mark.parametrize("body", [None, [], "<html>", {"skills": []}, {"skills": [], "activities": []},
                                      {"skills": "x", "activities": []}])
    def test_anything_else_is_not_a_reading(self, body):
        assert hiscores.parse(body) is None

    def test_slugs_match_the_ones_consumers_use(self):
        assert hiscores.metric_slug("Tombs of Amascut: Expert Mode") == "tombs_of_amascut_expert"
        assert hiscores.metric_slug("LMS - Rank") == "last_man_standing"
        assert hiscores.metric_slug("Rifts closed") == "guardians_of_the_rift"
        assert hiscores.metric_slug("Phosani's Nightmare") == "phosanis_nightmare"
        assert hiscores.metric_slug("TzKal-Zuk") == "tzkal_zuk"

    def test_every_wom_boss_and_activity_has_a_name_we_would_produce(self):
        wom = pytest.importorskip("wom")
        # The hiscores' own display names, as of 2026-11.
        names = ["Chambers of Xeric: Challenge Mode", "The Corrupted Gauntlet", "Vet'ion",
                 "Calvar'ion", "Kree'Arra", "Thermonuclear Smoke Devil", "Bounty Hunter - Hunter",
                 "PvP Arena - Rank", "Soul Wars Zeal", "Colosseum Glory", "Collections Logged"]
        known = {m.value for m in wom.Bosses} | {m.value for m in wom.Activities}
        assert [n for n in names if hiscores.metric_slug(n) not in known] == []

    def test_board_follows_the_account_type(self):
        assert hiscores.board_for("ironman").endswith("_ironman")
        assert hiscores.board_for("ultimate_ironman").endswith("_ultimate")
        assert hiscores.board_for("group_ironman") == hiscores.MAIN_BOARD
        assert hiscores.board_for(None) == hiscores.MAIN_BOARD
        assert "player=Lynx%20Titan" in hiscores.url_for("Lynx Titan")


class TestGate:
    @pytest.fixture(autouse=True)
    def _prod(self, monkeypatch):
        import utils.external_calls as external

        monkeypatch.setattr(external, "external_apis_allowed", lambda: True)

    def test_one_slot_per_interval(self):
        r = FakeRedis()
        assert hiscores.take_slot(r) and not hiscores.take_slot(r)

    def test_errors_open_the_breaker_and_a_success_resets_the_count(self):
        r = FakeRedis()
        for _ in range(hiscores.BREAKER_FAILURES - 1):
            hiscores.record_outcome(r, hiscores.ERROR)
        hiscores.record_outcome(r, hiscores.OK)
        for _ in range(hiscores.BREAKER_FAILURES - 1):
            hiscores.record_outcome(r, hiscores.ERROR)
        assert hiscores.may_request(r) is None
        hiscores.record_outcome(r, hiscores.ERROR)
        assert hiscores.may_request(r) == "breaker open"

    def test_a_missing_player_is_an_answer_not_a_failure(self):
        r = FakeRedis()
        for _ in range(20):
            hiscores.record_outcome(r, hiscores.NOT_FOUND)
        assert hiscores.may_request(r) is None

    def test_a_429_opens_the_breaker_at_once(self):
        r = FakeRedis()
        hiscores.record_outcome(r, hiscores.RATE_LIMITED, 30)
        assert hiscores.may_request(r) == "breaker open"

    def test_the_daily_cap_stops_requests(self):
        r = FakeRedis()
        r.data[hiscores._count_key()] = hiscores.DAILY_CAP
        assert hiscores.may_request(r) == "daily cap reached"

    def test_redis_down_means_no_requests(self):
        r = FakeRedis()
        r.broken = True
        assert hiscores.may_request(r) is not None and not hiscores.take_slot(r)

    def test_a_dev_instance_stays_silent(self, monkeypatch):
        import utils.external_calls as external

        monkeypatch.setattr(external, "external_apis_allowed", lambda: False)
        assert hiscores.may_request(FakeRedis()) == "dev instance"


def _due(**over):
    args = dict(starts_at=NOW - timedelta(days=3), ends_at=NOW + timedelta(days=3), joined_at=None,
                plugin_seen_at=None, last_polled=None, last_changed=None, first_polled=None,
                not_found_at=None, has_start=True, has_end=False)
    args.update(over)
    return schedule.due(NOW, **args)


class TestSchedule:
    def test_never_polled_is_due(self):
        assert _due().reason == schedule.ROTATION

    def test_plugin_covered_players_are_left_out_of_the_rotation(self):
        assert _due(plugin_seen_at=NOW - timedelta(hours=5)) is None
        assert _due(plugin_seen_at=NOW - timedelta(hours=7)) is not None

    def test_cadence_follows_activity(self):
        active = dict(first_polled=NOW - timedelta(days=5), last_changed=NOW - timedelta(hours=3))
        assert _due(last_polled=NOW - timedelta(hours=1), **active) is None
        assert _due(last_polled=NOW - timedelta(hours=2), **active) is not None
        quiet = dict(first_polled=NOW - timedelta(days=5), last_changed=NOW - timedelta(hours=48))
        assert _due(last_polled=NOW - timedelta(hours=5), **quiet) is None
        assert _due(last_polled=NOW - timedelta(hours=6), **quiet) is not None
        dormant = dict(first_polled=NOW - timedelta(days=5), last_changed=None)
        assert _due(last_polled=NOW - timedelta(hours=23), **dormant) is None
        assert _due(last_polled=NOW - timedelta(hours=24), **dormant) is not None

    def test_a_new_participant_is_treated_as_active_at_first(self):
        assert _due(last_polled=NOW - timedelta(hours=2), first_polled=NOW - timedelta(hours=2)) is not None

    def test_start_reading_is_pinned_and_taken_from_half_an_hour_before(self):
        start = NOW + timedelta(minutes=20)
        verdict = _due(starts_at=start, has_start=False)
        assert (verdict.reason, verdict.pinned) == (schedule.START, True)
        assert _due(starts_at=NOW + timedelta(minutes=45), has_start=False) is None

    def test_start_reading_is_wanted_even_from_plugin_users(self):
        verdict = _due(starts_at=NOW - timedelta(minutes=5), has_start=False,
                       plugin_seen_at=NOW - timedelta(minutes=1))
        assert verdict.reason == schedule.START

    def test_a_late_joiner_starts_when_they_joined(self):
        verdict = _due(has_start=False, joined_at=NOW - timedelta(hours=1))
        assert verdict.reason == schedule.START

    def test_a_start_missed_by_more_than_the_window_is_not_faked(self):
        assert _due(has_start=False).reason == schedule.ROTATION

    def test_end_reading_once_then_nothing(self):
        ended = dict(ends_at=NOW - timedelta(hours=1))
        verdict = _due(**ended)
        assert (verdict.reason, verdict.pinned) == (schedule.END, True)
        assert _due(has_end=True, **ended) is None
        assert _due(ends_at=NOW - timedelta(hours=13)) is None

    def test_a_name_the_hiscores_do_not_know_waits_a_day(self):
        assert _due(not_found_at=NOW - timedelta(hours=23)) is None
        assert _due(not_found_at=NOW - timedelta(hours=25)) is not None

    def test_order_is_end_then_start_then_most_overdue(self):
        a = (schedule.Due(schedule.ROTATION, False, 2, 10.0), "rot-small")
        b = (schedule.Due(schedule.ROTATION, False, 2, 900.0), "rot-big")
        c = (schedule.Due(schedule.START, True, 1, 1.0), "start")
        d = (schedule.Due(schedule.END, True, 0, 1.0), "end")
        assert [x[1] for x in schedule.order([a, b, c, d])] == ["end", "start", "rot-big", "rot-small"]


@pytest.fixture
def s():
    engine = create_engine("sqlite://")
    PlayerStatSnapshot.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _rec(s, skills, activities=None, *, when, pinned=False, total=None):
    return store.record_hiscores_snapshot(
        s, 7, skills, activities or {}, {"overall": 9}, total_xp=total, pinned=pinned, now=when)


def _rows(s):
    return s.query(PlayerStatSnapshot).order_by(PlayerStatSnapshot.taken_at).all()


class TestStoringReadings:
    def test_first_reading_is_written_with_only_what_was_ranked(self, s):
        assert _rec(s, {"attack": 1000, "magic": 2000}, {"zulrah": 5}, when=NOW) == store.WRITTEN
        (row,) = _rows(s)
        assert row.source == store.SOURCE_HISCORES and not row.flagged
        assert store.decode_skills(row.skills) == {"attack": 1000, "magic": 2000}
        assert store.decode_activities(row.activities) == {"zulrah": 5}
        assert row.total_xp == 3000

    def test_an_identical_reading_writes_nothing(self, s):
        _rec(s, {"attack": 1000}, when=NOW)
        assert _rec(s, {"attack": 1000}, when=NOW + timedelta(hours=2)) == store.UNCHANGED
        assert len(_rows(s)) == 1

    def test_a_pinned_reading_is_written_even_when_identical(self, s):
        _rec(s, {"attack": 1000}, when=NOW)
        assert _rec(s, {"attack": 1000}, when=NOW + timedelta(hours=2), pinned=True) == store.WRITTEN
        assert [r.pinned for r in _rows(s)] == [False, True]

    def test_xp_going_down_is_flagged_and_never_a_baseline(self, s):
        _rec(s, {"attack": 5000}, when=NOW)
        assert _rec(s, {"attack": 4000}, when=NOW + timedelta(hours=2)) == store.FLAGGED
        assert [r.flagged for r in _rows(s)] == [False, True]
        assert store.snapshot_at(s, 7, NOW + timedelta(days=1)).total_xp == 5000

    def test_a_kill_count_going_down_is_flagged(self, s):
        _rec(s, {"attack": 5000}, {"zulrah": 50}, when=NOW)
        assert _rec(s, {"attack": 5100}, {"zulrah": 40}, when=NOW + timedelta(hours=2)) == store.FLAGGED

    def test_an_impossible_jump_is_flagged_but_a_long_gap_is_not(self, s):
        _rec(s, {"attack": 1_000_000}, when=NOW)
        assert _rec(s, {"attack": 40_000_000}, when=NOW + timedelta(hours=2)) == store.FLAGGED
        assert _rec(s, {"attack": 40_000_000}, when=NOW + timedelta(days=30)) == store.FLAGGED  # same reading
        assert _rec(s, {"attack": 41_000_000}, when=NOW + timedelta(days=31)) == store.WRITTEN

    def test_a_repeated_flagged_reading_is_not_stored_again(self, s):
        _rec(s, {"attack": 5000}, when=NOW)
        _rec(s, {"attack": 4000}, when=NOW + timedelta(hours=2))
        assert _rec(s, {"attack": 4000}, when=NOW + timedelta(hours=4), pinned=True) == store.FLAGGED
        assert len(_rows(s)) == 2

    def test_a_flagged_reading_is_never_pinned(self, s):
        _rec(s, {"attack": 5000}, when=NOW)
        _rec(s, {"attack": 4000}, when=NOW + timedelta(hours=2), pinned=True)
        assert [r.pinned for r in _rows(s)] == [False, False]

    def test_a_skill_newly_ranked_is_not_a_jump(self, s):
        _rec(s, {"attack": 1000}, when=NOW)
        assert _rec(s, {"attack": 1000, "magic": 9_000_000}, when=NOW + timedelta(hours=1)) == store.WRITTEN

    def test_hiscores_behind_the_plugin_are_old_news(self, s):
        xp = {**{skill: 0 for skill in SKILLS}, "attack": 9000}
        store.record_plugin_snapshot(s, 7, xp, now=NOW)
        assert _rec(s, {"attack": 8000}, when=NOW + timedelta(hours=1), total=8000) == store.STALE
        assert [r.source for r in _rows(s)] == ["plugin"]

    def test_a_stale_pinned_reading_pins_the_plugin_row_instead(self, s):
        xp = {**{skill: 0 for skill in SKILLS}, "attack": 9000}
        store.record_plugin_snapshot(s, 7, xp, now=NOW)
        _rec(s, {"attack": 8000}, when=NOW + timedelta(hours=1), total=8000, pinned=True)
        assert _rows(s)[0].pinned is True

    def test_hiscores_ahead_of_the_plugin_are_stored(self, s):
        xp = {**{skill: 0 for skill in SKILLS}, "attack": 9000}
        store.record_plugin_snapshot(s, 7, xp, now=NOW)
        assert _rec(s, {"attack": 9500}, when=NOW + timedelta(hours=8), total=9500) == store.WRITTEN

    def test_pin_latest(self, s):
        assert store.pin_latest(s, 7) is False
        _rec(s, {"attack": 1000}, when=NOW)
        assert store.pin_latest(s, 7) and _rows(s)[0].pinned


def _participant(**over):
    base = {"player_id": 7, "name": "Some Player", "account_type": None, "account_varbit": None,
            "event_id": 1, "starts_at": NOW - timedelta(days=3), "ends_at": NOW + timedelta(days=3),
            "joined_at": None, "plugin_seen_at": None}
    base.update(over)
    return base


class TestPlan:
    def test_one_lookup_per_player_for_the_most_urgent_reason(self):
        r = FakeRedis()
        r.hset(poller._pin_key(1, 7), "start", 1)
        people = [_participant(),
                  _participant(event_id=2, ends_at=NOW - timedelta(hours=1)),
                  _participant(player_id=8, plugin_seen_at=NOW - timedelta(hours=1))]
        r.hset(poller._pin_key(1, 8), "start", 1)
        queue = poller.plan(r, people, NOW)
        assert [(d.reason, p["player_id"], p["pins"]) for d, p in queue] == [("end", 7, [(2, "end")])]

    def test_state_in_redis_decides_who_is_due(self):
        r = FakeRedis()
        r.hset(poller._pin_key(1, 7), "start", 1)
        stamp = (NOW - timedelta(minutes=30) - datetime(1970, 1, 1)).total_seconds()
        r.hset(poller._state_key(7), mapping={"polled": stamp, "first": stamp})
        assert poller.plan(r, [_participant()], NOW) == []


class TestPollPlayer:
    def _run(self, monkeypatch, result, outcome="written", **participant):
        r = FakeRedis()
        calls = {"store": 0, "boards": []}

        async def fake_fetch(http, redis_conn, name, board, **kwargs):
            calls["boards"].append(board)
            return result if not isinstance(result, list) else result[len(calls["boards"]) - 1]

        def fake_store(player_id, varbit, reading, pinned, now):
            calls["store"] += 1
            calls["pinned"] = pinned
            return outcome

        monkeypatch.setattr(poller.hiscores, "fetch", fake_fetch)
        monkeypatch.setattr(poller, "_store_db", fake_store)
        monkeypatch.setattr(poller, "_pin_latest_db", lambda player_id: True)
        p = _participant(pins=[(1, "start")], **participant)
        due = schedule.Due(schedule.START, True, 1)
        got = asyncio.run(poller.poll_player(None, r, p, due, NOW))
        return got, r, calls

    def test_a_reading_is_stored_and_the_pin_remembered(self, monkeypatch):
        ok = hiscores.FetchResult(hiscores.OK, hiscores.parse(_payload()))
        got, r, calls = self._run(monkeypatch, ok)
        assert got == "written" and calls["pinned"] is True
        assert "start" in r.hgetall(poller._pin_key(1, 7))
        state = r.hgetall(poller._state_key(7))
        assert "polled" in state and "first" in state and "changed" not in state

    def test_a_later_changed_reading_marks_the_player_active(self, monkeypatch):
        ok = hiscores.FetchResult(hiscores.OK, hiscores.parse(_payload()))
        r = FakeRedis()
        r.hset(poller._state_key(7), mapping={"polled": 1, "first": 1})

        async def fake_fetch(*a, **k):
            return ok

        monkeypatch.setattr(poller.hiscores, "fetch", fake_fetch)
        monkeypatch.setattr(poller, "_store_db", lambda *a: "written")
        asyncio.run(poller.poll_player(None, r, _participant(pins=[]),
                                       schedule.Due(schedule.ROTATION, False, 2), NOW))
        assert "changed" in r.hgetall(poller._state_key(7))

    def test_not_found_is_remembered_and_nothing_stored(self, monkeypatch):
        got, r, calls = self._run(monkeypatch, hiscores.FetchResult(hiscores.NOT_FOUND))
        assert got == "not_found" and calls["store"] == 0
        assert "not_found" in r.hgetall(poller._state_key(7))
        assert r.hgetall(poller._pin_key(1, 7)) == {}

    def test_an_error_leaves_no_trace_so_it_is_retried(self, monkeypatch):
        got, r, calls = self._run(monkeypatch, hiscores.FetchResult(hiscores.ERROR))
        assert got == "error" and calls["store"] == 0 and r.hashes == {}

    def test_ironman_board_first_then_the_main_board(self, monkeypatch):
        ok = hiscores.FetchResult(hiscores.OK, hiscores.parse(_payload()))
        got, r, calls = self._run(monkeypatch, [hiscores.FetchResult(hiscores.NOT_FOUND), ok],
                                  account_type="ironman")
        assert got == "written"
        assert calls["boards"] == [hiscores.BOARDS["ironman"], hiscores.MAIN_BOARD]

    def test_a_plugin_user_gets_their_own_row_pinned_and_no_request(self, monkeypatch):
        got, r, calls = self._run(monkeypatch, hiscores.FetchResult(hiscores.ERROR),
                                  plugin_seen_at=NOW - timedelta(minutes=5))
        assert got == "plugin_pinned" and calls["boards"] == []
        assert "start" in r.hgetall(poller._pin_key(1, 7))
