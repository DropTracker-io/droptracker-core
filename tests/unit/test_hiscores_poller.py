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
        self.hashes.pop(key, None)

    def lpush(self, key, value):
        self.data.setdefault(key, [])
        self.data[key].insert(0, value)

    def ltrim(self, key, start, stop):
        self.data[key] = self.data.get(key, [])[start:stop + 1]

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

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hincrby(self, key, field, amount=1):
        target = self.hashes.setdefault(key, {})
        target[field] = str(int(target.get(field, 0)) + amount)
        return int(target[field])

    def zadd(self, key, mapping, nx=False, xx=False):
        target = self.hashes.setdefault("z:" + key, {})
        for member, score in mapping.items():
            if (nx and member in target) or (xx and member not in target):
                continue
            target[member] = float(score)

    def zrange(self, key, start, stop, withscores=False):
        items = self.hashes.get("z:" + key, {})
        return list(items.items()) if withscores else list(items)

    def zrem(self, key, *members):
        for member in members:
            self.hashes.get("z:" + key, {}).pop(member, None)

    def zrangebyscore(self, key, low, high, start=0, num=None):
        items = sorted(self.hashes.get("z:" + key, {}).items(), key=lambda kv: (kv[1], kv[0]))
        hits = [m for m, score in items if score <= float(high)]
        return hits[start:start + num] if num is not None else hits

    def zcount(self, key, low, high):
        return len(self.zrangebyscore(key, low, high))

    def zscore(self, key, member):
        return self.hashes.get("z:" + key, {}).get(member)


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
                plugin_synced_at=None, last_polled=None, last_changed=None, first_polled=None,
                not_found_at=None, has_start=True, has_end=False)
    args.update(over)
    return schedule.due(NOW, **args)


class TestSchedule:
    def test_never_polled_is_due(self):
        assert _due().reason == schedule.ROTATION

    def test_players_whose_stats_are_arriving_are_left_out_of_the_rotation(self):
        assert _due(plugin_synced_at=NOW - timedelta(minutes=15)) is None
        # Some while after the last sync they have probably logged out: read them.
        assert _due(plugin_synced_at=NOW - timedelta(minutes=25)) is not None

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

    def test_start_reading_is_pinned_and_taken_once_scoring_has_begun(self):
        # Gains count from the first reading INSIDE the window, so a reading
        # before the start would be no use as the baseline.
        assert _due(starts_at=NOW + timedelta(minutes=5), has_start=False) is None
        verdict = _due(starts_at=NOW - timedelta(minutes=2), has_start=False)
        assert (verdict.reason, verdict.pinned) == (schedule.START, True)

    def test_start_reading_is_wanted_even_from_plugin_users(self):
        verdict = _due(starts_at=NOW - timedelta(minutes=5), has_start=False,
                       plugin_synced_at=NOW - timedelta(minutes=1))
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

    def test_a_stale_pinned_reading_repeats_the_plugin_numbers_at_that_moment(self, s):
        xp = {**{skill: 0 for skill in SKILLS}, "attack": 9000}
        store.record_plugin_snapshot(s, 7, xp, now=NOW)
        when = NOW + timedelta(hours=1)
        assert _rec(s, {"attack": 8000}, when=when, total=8000, pinned=True) == store.STALE
        first, carried = _rows(s)
        assert (carried.source, carried.taken_at, carried.pinned) == ("plugin", when, True)
        assert carried.skills == first.skills and first.pinned is False

    def test_carry_forward_needs_a_plugin_reading(self, s):
        assert store.carry_forward(s, 7, NOW) is False
        _rec(s, {"attack": 1000}, when=NOW - timedelta(hours=1))
        assert store.carry_forward(s, 7, NOW) is False and len(_rows(s)) == 1

    def test_a_carried_row_is_not_overwritten_by_the_next_sync(self, s):
        xp = {**{skill: 0 for skill in SKILLS}, "attack": 9000}
        store.record_plugin_snapshot(s, 7, xp, now=NOW - timedelta(minutes=30))
        assert store.carry_forward(s, 7, NOW)
        store.record_plugin_snapshot(s, 7, {**xp, "attack": 9500}, now=NOW + timedelta(minutes=2))
        assert [(r.pinned, store.decode_skills(r.skills)["attack"]) for r in _rows(s)] == [
            (False, 9000), (True, 9000), (False, 9500)]

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
            "joined_at": None, "plugin_synced_at": None}
    base.update(over)
    return base


class TestPlan:
    def test_one_lookup_per_player_for_the_most_urgent_reason(self):
        r = FakeRedis()
        r.hset(poller._pin_key(1, 7), "start", 1)
        people = [_participant(),
                  _participant(event_id=2, ends_at=NOW - timedelta(hours=1)),
                  _participant(player_id=8, plugin_synced_at=NOW - timedelta(minutes=10))]
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

    def test_a_plugin_user_gets_a_start_row_of_their_own_and_no_request(self, monkeypatch):
        carried = []
        monkeypatch.setattr(poller, "_carry_forward_db", lambda pid, now: carried.append(pid) or True)
        got, r, calls = self._run(monkeypatch, hiscores.FetchResult(hiscores.ERROR),
                                  plugin_synced_at=NOW - timedelta(minutes=5))
        assert got == "plugin_pinned" and calls["boards"] == [] and carried == [7]
        assert "start" in r.hgetall(poller._pin_key(1, 7))

    def test_a_plugin_user_at_the_end_keeps_their_latest_row(self, monkeypatch):
        r = FakeRedis()
        pinned = []
        monkeypatch.setattr(poller, "_pin_latest_db", lambda pid: pinned.append(pid) or True)
        monkeypatch.setattr(poller, "_carry_forward_db", lambda pid, now: pytest.fail("not at an end reading"))
        p = _participant(pins=[(1, "end")], plugin_synced_at=NOW - timedelta(minutes=5))
        got = asyncio.run(poller.poll_player(None, r, p, schedule.Due(schedule.END, True, 0), NOW))
        assert got == "plugin_pinned" and pinned == [7]


class TestAccountType:
    """A player the plugin never told us about: which boards are they on?"""

    def _reading(self, xp):
        return hiscores.Hiscores(skills={"attack": xp}, total_xp=xp)

    def _detect(self, main_xp, boards):
        asked = []

        async def fetch_board(board):
            asked.append(board)
            xp = boards.get(board)
            if xp == "down":
                return hiscores.FetchResult(hiscores.ERROR, board=board)
            if xp is None:
                return hiscores.FetchResult(hiscores.NOT_FOUND, board=board)
            return hiscores.FetchResult(hiscores.OK, self._reading(xp), board)

        return asyncio.run(hiscores.detect_account_type(fetch_board, self._reading(main_xp))), asked

    IRON, HC, UIM = (hiscores.BOARDS["ironman"], hiscores.BOARDS["hardcore_ironman"],
                     hiscores.BOARDS["ultimate_ironman"])

    def test_not_on_the_ironman_board_is_a_normal_account_in_one_request(self):
        assert self._detect(5000, {}) == ("normal", [self.IRON])

    def test_ironman(self):
        assert self._detect(5000, {self.IRON: 5000}) == ("ironman", [self.IRON, self.HC, self.UIM])

    def test_hardcore_and_ultimate(self):
        assert self._detect(5000, {self.IRON: 5000, self.HC: 5000})[0] == "hardcore_ironman"
        assert self._detect(5000, {self.IRON: 5000, self.UIM: 5000})[0] == "ultimate_ironman"

    def test_a_dead_hardcore_is_a_plain_ironman(self):
        # The hardcore entry stopped at death; the ironman one kept going.
        assert self._detect(9000, {self.IRON: 9000, self.HC: 4000})[0] == "ironman"

    def test_a_de_ironed_account_is_normal(self):
        assert self._detect(9000, {self.IRON: 6000}) == ("normal", [self.IRON])

    def test_a_board_that_cannot_be_read_gives_no_answer(self):
        assert self._detect(5000, {self.IRON: "down"})[0] is None
        assert self._detect(5000, {self.IRON: 5000, self.HC: "down"})[0] is None
        assert self._detect(5000, {self.IRON: 5000, self.UIM: "down"})[0] is None

    def _poll(self, monkeypatch, participant=None, state=None, boards=None):
        r = FakeRedis()
        if state:
            r.hset(poller._state_key(7), mapping=state)
        asked, stored = [], {}
        boards = boards or {}

        async def fake_fetch(http, redis_conn, name, board, **kwargs):
            asked.append(board)
            if board == hiscores.MAIN_BOARD or board in boards:
                return hiscores.FetchResult(hiscores.OK, self._reading(5000), board)
            return hiscores.FetchResult(hiscores.NOT_FOUND, board=board)

        def fake_store(player_id, varbit, reading, pinned, now):
            stored["varbit"] = varbit
            return "written"

        monkeypatch.setattr(poller.hiscores, "fetch", fake_fetch)
        monkeypatch.setattr(poller, "_store_db", fake_store)
        asyncio.run(poller.poll_player(None, r, _participant(pins=[], **(participant or {})),
                                       schedule.Due(schedule.ROTATION, False, 2), NOW))
        return r.hgetall(poller._state_key(7)), asked, stored

    def test_an_unknown_player_is_typed_once_and_the_answer_kept(self, monkeypatch):
        state, asked, stored = self._poll(monkeypatch, boards={self.IRON})
        assert state["type"] == "ironman" and "type_at" in state
        assert asked == [hiscores.MAIN_BOARD, self.IRON, self.HC, self.UIM]
        assert stored["varbit"] == 1          # ironman, as the plugin's varbit would say

    def test_a_kept_answer_is_used_and_costs_nothing_more(self, monkeypatch):
        stamp = (NOW - timedelta(days=3) - datetime(1970, 1, 1)).total_seconds()
        state, asked, stored = self._poll(monkeypatch, state={"type": "ironman", "type_at": stamp, "first": 1},
                                          boards={self.IRON})
        assert asked == [self.IRON] and stored["varbit"] == 1

    def test_an_old_answer_is_checked_again(self, monkeypatch):
        stamp = (NOW - timedelta(days=40) - datetime(1970, 1, 1)).total_seconds()
        state, asked, stored = self._poll(monkeypatch, state={"type": "ironman", "type_at": stamp, "first": 1})
        assert asked == [hiscores.MAIN_BOARD, self.IRON] and state["type"] == "normal"
        assert stored["varbit"] == 0

    def test_a_type_the_plugin_told_us_is_never_second_guessed(self, monkeypatch):
        state, asked, stored = self._poll(
            monkeypatch, participant={"account_type": "ultimate_ironman", "account_varbit": 2},
            boards={self.UIM})
        assert asked == [self.UIM] and "type" not in state and stored["varbit"] == 2

    def test_group_ironman_from_the_plugin_stays_group_ironman(self, monkeypatch):
        state, asked, stored = self._poll(
            monkeypatch, participant={"account_type": "group_ironman", "account_varbit": 4})
        assert asked == [hiscores.MAIN_BOARD] and stored["varbit"] == 4


class TestBackgroundCadence:
    def _wait(self, **over):
        args = dict(last_changed=None, first_polled=NOW - timedelta(days=1), not_found_streak=0)
        args.update(over)
        return schedule.background_interval(NOW, **args)

    def test_follows_how_recently_a_reading_changed(self):
        assert self._wait(last_changed=NOW - timedelta(days=2)) == timedelta(hours=3)
        assert self._wait(last_changed=NOW - timedelta(days=10)) == timedelta(hours=12)
        assert self._wait(last_changed=NOW - timedelta(days=20)) == timedelta(days=1)
        assert self._wait(last_changed=NOW - timedelta(days=40)) == timedelta(days=3)

    def test_one_reading_is_not_evidence_of_activity(self):
        assert self._wait() == timedelta(days=1)
        assert self._wait(first_polled=NOW - timedelta(days=45)) == timedelta(days=3)

    def test_unknown_names_back_off_to_a_month(self):
        waits = [self._wait(not_found_streak=n) for n in (1, 2, 3, 4, 9)]
        assert waits == [timedelta(days=3), timedelta(days=6), timedelta(days=12),
                         timedelta(days=24), timedelta(days=30)]


class TestPace:
    """The request interval tunes itself: faster when clean, slower when not."""

    def _hour(self, r, answered, failed, when):
        r.hashes[hiscores.WINDOW_KEY] = {"answered": str(answered), "failed": str(failed)}
        return hiscores.tune_pace(r, when)

    def _started(self):
        r = FakeRedis()
        assert hiscores.tune_pace(r, NOW) is None          # first call only starts the clock
        return r

    def test_starts_at_the_configured_interval(self):
        assert hiscores.current_interval(FakeRedis()) == hiscores.INTERVAL_MS

    def test_a_clean_hour_undoes_a_slow_down(self):
        r = self._started()
        line = self._hour(r, 900, 1, NOW + timedelta(hours=1))
        assert hiscores.current_interval(r) == round(hiscores.INTERVAL_MS / hiscores.SLOW_DOWN_FACTOR)
        assert "clean" not in line and str(hiscores.INTERVAL_MS) in line
        assert "clean hour" in r.data[hiscores.PACE_LOG_KEY][0]

    def test_it_never_goes_below_the_floor(self):
        r = self._started()
        r.set(hiscores.INTERVAL_KEY, hiscores.MIN_INTERVAL_MS)
        assert self._hour(r, 900, 0, NOW + timedelta(hours=1)) is None
        assert hiscores.current_interval(r) == hiscores.MIN_INTERVAL_MS

    def test_at_most_one_change_an_hour(self):
        r = self._started()
        self._hour(r, 900, 0, NOW + timedelta(hours=1))
        assert self._hour(r, 900, 0, NOW + timedelta(hours=1, minutes=30)) is None
        assert hiscores.current_interval(r) == round(hiscores.INTERVAL_MS / hiscores.SLOW_DOWN_FACTOR)

    def test_too_few_requests_say_nothing(self):
        r = self._started()
        assert self._hour(r, 50, 0, NOW + timedelta(hours=1)) is None
        assert hiscores.current_interval(r) == hiscores.INTERVAL_MS

    def test_a_rising_failure_share_slows_down(self):
        r = self._started()
        r.set(hiscores.INTERVAL_KEY, 600)
        self._hour(r, 950, 50, NOW + timedelta(hours=1))
        assert hiscores.current_interval(r) == 900

    def test_failures_never_slow_past_the_starting_pace(self):
        """2026-11-20: five hours of 502s took 500 ms to 5.7 s."""
        r = self._started()
        r.set(hiscores.INTERVAL_KEY, 600)
        for hour in range(1, 9):
            self._hour(r, 900, 100, NOW + timedelta(hours=hour))
        assert hiscores.current_interval(r) == hiscores.INTERVAL_MS

    def test_failures_do_not_speed_up_a_pace_a_refusal_set(self):
        r = self._started()
        r.set(hiscores.INTERVAL_KEY, 8000)
        self._hour(r, 900, 100, NOW + timedelta(hours=1))
        assert hiscores.current_interval(r) == 8000

    def test_recovery_from_an_outage_takes_hours_not_days(self):
        r = self._started()
        r.set(hiscores.INTERVAL_KEY, hiscores.INTERVAL_MS)
        hours = 0
        while hiscores.current_interval(r) > hiscores.MIN_INTERVAL_MS:
            hours += 1
            self._hour(r, 900, 0, NOW + timedelta(hours=hours))
        assert hours <= 5

    def test_a_middling_hour_holds(self):
        r = self._started()
        assert self._hour(r, 985, 15, NOW + timedelta(hours=1)) is None
        assert hiscores.current_interval(r) == hiscores.INTERVAL_MS

    def test_a_refusal_doubles_the_interval_at_once(self):
        r = FakeRedis()
        hiscores.record_health(r, hiscores.ERROR, "http 403", NOW)
        assert hiscores.current_interval(r) == hiscores.INTERVAL_MS * 2
        hiscores.record_health(r, hiscores.RATE_LIMITED, "", NOW)
        assert hiscores.current_interval(r) == hiscores.INTERVAL_MS * 4
        assert "refused (http 429)" in r.data[hiscores.PACE_LOG_KEY][0]

    def test_a_502_does_not_count_as_a_refusal(self):
        r = FakeRedis()
        hiscores.record_health(r, hiscores.ERROR, "http 502", NOW)
        assert hiscores.current_interval(r) == hiscores.INTERVAL_MS

    def test_no_speeding_up_for_twelve_hours_after_a_refusal(self):
        r = self._started()
        hiscores.record_health(r, hiscores.ERROR, "http 403", NOW)
        slowed = hiscores.current_interval(r)
        assert self._hour(r, 900, 0, NOW + timedelta(hours=2)) is None
        assert hiscores.current_interval(r) == slowed
        self._hour(r, 900, 0, NOW + timedelta(hours=13))
        # ...and for a week after it, only in small steps.
        assert hiscores.current_interval(r) == slowed - hiscores.SPEED_UP_STEP_MS
        self._hour(r, 900, 0, NOW + timedelta(days=8))
        assert hiscores.current_interval(r) == round((slowed - hiscores.SPEED_UP_STEP_MS) / hiscores.SLOW_DOWN_FACTOR)

    def test_the_interval_is_capped_both_ways(self):
        r = FakeRedis()
        r.set(hiscores.INTERVAL_KEY, 5)
        assert hiscores.current_interval(r) == hiscores.MIN_INTERVAL_MS
        r.set(hiscores.INTERVAL_KEY, 10 ** 9)
        assert hiscores.current_interval(r) == hiscores.MAX_INTERVAL_MS
        r.set(hiscores.INTERVAL_KEY, "junk")
        assert hiscores.current_interval(r) == hiscores.INTERVAL_MS

    def test_outcomes_feed_the_window(self):
        r = FakeRedis()
        hiscores.record_health(r, hiscores.OK, "", NOW)
        hiscores.record_health(r, hiscores.NOT_FOUND, "", NOW)
        hiscores.record_health(r, hiscores.ERROR, "TimeoutError", NOW)
        assert r.hashes[hiscores.WINDOW_KEY] == {"answered": "2", "failed": "1"}

    def test_the_user_agent_says_who_and_where(self):
        assert "DropTracker" in hiscores.USER_AGENT and "droptracker.io" in hiscores.USER_AGENT


class TestBackgroundQueue:
    def test_seeding_adds_the_missing_real_accounts_first_and_drops_the_gone(self):
        r = FakeRedis()
        r.zadd(poller.QUEUE_KEY, {"1": 999.0, "50": 5.0})
        out = poller.seed_queue(r, [(1, False), (2, False), (3, True)])
        assert out == {"added": 2, "removed": 1}
        assert r.zscore(poller.QUEUE_KEY, "1") == 999.0      # keeps its place
        assert r.zrangebyscore(poller.QUEUE_KEY, "-inf", 10) == ["2", "3"]

    def test_rebase_pulls_forward_and_never_pushes_back(self):
        r = FakeRedis()
        epoch = lambda t: (t - datetime(1970, 1, 1)).total_seconds()
        # Read 30h ago, never seen to change: now due daily, was booked 2 days out.
        r.hset(poller._state_key(1), mapping={"polled": epoch(NOW - timedelta(hours=30)),
                                              "first": epoch(NOW - timedelta(hours=30))})
        r.zadd(poller.QUEUE_KEY, {"1": epoch(NOW + timedelta(days=2))})
        # Already due sooner than the cadence would say: left alone.
        r.hset(poller._state_key(2), mapping={"polled": epoch(NOW - timedelta(hours=1)),
                                              "first": epoch(NOW - timedelta(hours=1))})
        r.zadd(poller.QUEUE_KEY, {"2": epoch(NOW + timedelta(hours=2)), "3": 0.0})
        assert poller.rebase_queue(r, NOW) == {"moved_earlier": 1, "unchanged": 2}
        assert r.zscore(poller.QUEUE_KEY, "1") == epoch(NOW - timedelta(hours=6))
        assert r.zscore(poller.QUEUE_KEY, "2") == epoch(NOW + timedelta(hours=2))

    def _run(self, monkeypatch, players, outcome="written", slots=5, used_today=0):
        r = FakeRedis()
        polled = []

        async def fake_poll(http, redis_conn, participant, due, now=None):
            polled.append(participant["player_id"])
            return outcome

        monkeypatch.setattr(poller, "_last_seed", 10 ** 12)   # no seeding in these tests
        monkeypatch.setattr(poller, "_last_session_scan", 10 ** 12)
        monkeypatch.setattr(poller, "poll_player", fake_poll)
        monkeypatch.setattr(poller, "_players_db", lambda ids: {i: players[i] for i in ids if i in players})
        monkeypatch.setattr(poller.hiscores, "requests_today", lambda conn, now=None: used_today)
        for pid in (1, 2, 3):
            r.zadd(poller.QUEUE_KEY, {str(pid): float(pid)})
        stats = {"polled": 0}
        import time as _time

        monkeypatch.setattr(poller, "CONCURRENCY", 1)      # keep the order checkable
        rotation = schedule.Due(schedule.ROTATION, False, 2)

        async def go():
            found = await poller.background_items(r, slots, set(), stats)
            await poller.work(None, r, [("bg", rotation, p) for p in found],
                              _time.monotonic() + 30, stats)

        asyncio.run(go())
        return r, polled, stats

    def _player(self, pid, seen=None):
        return {"player_id": pid, "name": f"p{pid}", "account_type": None, "account_varbit": None,
                "plugin_synced_at": seen, "pins": []}

    def test_longest_waiting_first_and_each_is_rescheduled(self, monkeypatch):
        import time as _time

        players = {i: self._player(i) for i in (1, 2, 3)}
        r, polled, stats = self._run(monkeypatch, players, slots=2)
        assert polled == [1, 2] and stats["polled"] == 2
        assert r.zscore(poller.QUEUE_KEY, "1") > _time.time() + 3600
        assert r.zscore(poller.QUEUE_KEY, "3") == 3.0

    def test_a_player_whose_stats_are_arriving_costs_no_request_and_is_revisited_soon(self, monkeypatch):
        import time as _time
        from datetime import datetime as _dt

        players = {1: self._player(1, seen=_dt.utcnow() - timedelta(minutes=10)),
                   2: self._player(2), 3: self._player(3)}
        r, polled, stats = self._run(monkeypatch, players)
        assert polled == [2, 3] and stats["bg_covered"] == 1
        # Back 5 minutes after the window from that sync: about 15 minutes from now.
        wait = r.zscore(poller.QUEUE_KEY, "1") - _time.time()
        assert 12 * 60 < wait < 18 * 60

    def test_a_player_who_stopped_syncing_is_read(self, monkeypatch):
        from datetime import datetime as _dt

        players = {1: self._player(1, seen=_dt.utcnow() - timedelta(hours=3)),
                   2: self._player(2), 3: self._player(3)}
        _, polled, stats = self._run(monkeypatch, players)
        assert polled == [1, 2, 3] and "bg_covered" not in stats

    def test_a_deleted_player_leaves_the_queue(self, monkeypatch):
        r, polled, _ = self._run(monkeypatch, {2: self._player(2), 3: self._player(3)})
        assert polled == [2, 3] and r.zscore(poller.QUEUE_KEY, "1") is None

    def test_the_background_budget_is_respected(self, monkeypatch):
        players = {i: self._player(i) for i in (1, 2, 3)}
        _, polled, _ = self._run(monkeypatch, players, used_today=poller.BACKGROUND_DAILY)
        assert polled == []

    def test_a_block_stops_the_pass_and_keeps_everyone_queued(self, monkeypatch):
        players = {i: self._player(i) for i in (1, 2, 3)}
        r, polled, stats = self._run(monkeypatch, players, outcome="blocked")
        assert polled == [1] and stats["polled"] == 0 and r.zscore(poller.QUEUE_KEY, "1") == 1.0

    def test_an_error_is_retried_in_an_hour_not_at_once(self, monkeypatch):
        import time as _time

        players = {i: self._player(i) for i in (1, 2, 3)}
        r, polled, _ = self._run(monkeypatch, players, outcome="error")
        assert polled == [1, 2, 3]
        assert 3000 < r.zscore(poller.QUEUE_KEY, "1") - _time.time() < 3700


class TestHealth:
    """What scripts/health_watch.py is told. Silence is the only sign of a block."""

    def _checks(self, r, now=NOW):
        return {key: (ok, detail) for key, _label, ok, detail in hiscores.health_checks(r, now)}

    def _fail(self, r, detail, when, times=1):
        for _ in range(times):
            hiscores.record_health(r, hiscores.ERROR, detail, when)

    def test_nothing_to_report_where_the_poller_never_ran(self):
        assert hiscores.health_checks(FakeRedis(), NOW) == []

    def test_healthy(self):
        r = FakeRedis()
        hiscores.record_cycle(r, "running", NOW)
        hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(minutes=1))
        assert all(ok for ok, _ in self._checks(r).values()) and len(self._checks(r)) == 4

    def test_a_dead_loop_is_reported(self):
        r = FakeRedis()
        hiscores.record_cycle(r, "running", NOW - timedelta(minutes=20))
        ok, detail = self._checks(r)["hiscores:loop"]
        assert not ok and "droptracker-player-updates" in detail

    def test_refusals_page_after_half_an_hour_and_say_block(self):
        r = FakeRedis()
        hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(minutes=50))
        self._fail(r, "http 403", NOW - timedelta(minutes=10), times=6)
        hiscores.record_cycle(r, "breaker open", NOW)
        ok, detail = self._checks(r)["hiscores:answers"]
        assert not ok and "BLOCK" in detail and "http 403 x6" in detail and "HISCORES_POLLER=false" in detail

    def test_refusals_inside_the_first_half_hour_do_not_page_yet(self):
        r = FakeRedis()
        hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(minutes=20))
        self._fail(r, "http 403", NOW - timedelta(minutes=5), times=6)
        hiscores.record_cycle(r, "running", NOW)
        assert self._checks(r)["hiscores:answers"][0]

    def test_a_429_counts_as_a_refusal(self):
        r = FakeRedis()
        hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(minutes=50))
        hiscores.record_health(r, hiscores.RATE_LIMITED, "", NOW - timedelta(minutes=10))
        hiscores.record_cycle(r, "breaker open", NOW)
        ok, detail = self._checks(r)["hiscores:answers"]
        assert not ok and "http 429" in detail

    def test_an_outage_gets_three_hours_and_is_not_called_a_block(self):
        r = FakeRedis()
        hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(hours=2))
        self._fail(r, "http 503", NOW - timedelta(minutes=10), times=6)
        self._fail(r, "TimeoutError", NOW - timedelta(minutes=9), times=3)
        hiscores.record_cycle(r, "running", NOW)
        assert self._checks(r)["hiscores:answers"][0]
        later = NOW + timedelta(hours=2)
        self._fail(r, "http 503", later - timedelta(minutes=5), times=2)
        hiscores.record_cycle(r, "running", later)
        ok, detail = self._checks(r, later)["hiscores:answers"]
        assert not ok and "BLOCK" not in detail and "update" in detail

    def test_an_answer_clears_it(self):
        r = FakeRedis()
        hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(hours=1))
        self._fail(r, "http 403", NOW - timedelta(minutes=40), times=6)
        hiscores.record_health(r, hiscores.NOT_FOUND, "", NOW - timedelta(minutes=1))
        hiscores.record_cycle(r, "running", NOW)
        assert self._checks(r)["hiscores:answers"][0]

    def test_a_quiet_poller_is_not_a_silent_one(self):
        # Nothing was due for hours: no failures, so no page.
        r = FakeRedis()
        hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(hours=9))
        hiscores.record_cycle(r, "daily cap reached", NOW)
        assert all(ok for ok, _ in self._checks(r).values())

    def test_switched_off_reports_only_that_the_loop_is_alive(self):
        r = FakeRedis()
        self._fail(r, "http 403", NOW - timedelta(hours=5), times=9)
        hiscores.record_cycle(r, "disabled", NOW)
        assert list(self._checks(r)) == ["hiscores:loop"]

    def test_a_high_error_share_is_reported_even_while_answers_arrive(self):
        r = FakeRedis()
        hiscores.record_cycle(r, "running", NOW)
        for _ in range(25):
            hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(minutes=3))
        self._fail(r, "http 503", NOW - timedelta(minutes=2), times=30)
        hiscores.record_health(r, hiscores.OK, "", NOW - timedelta(minutes=1))
        checks = self._checks(r)
        assert checks["hiscores:answers"][0]
        ok, detail = checks["hiscores:errors"]
        assert not ok and "30 of 56" in detail

    def test_a_few_errors_among_few_requests_are_not_a_share(self):
        r = FakeRedis()
        hiscores.record_cycle(r, "running", NOW)
        hiscores.record_health(r, hiscores.OK, "", NOW)
        self._fail(r, "TimeoutError", NOW, times=5)
        hiscores.record_health(r, hiscores.OK, "", NOW)
        assert self._checks(r)["hiscores:errors"][0]

    def test_counts_span_the_hour_boundary(self):
        r = FakeRedis()
        edge = datetime(2026, 11, 12, 13, 2)
        self._fail(r, "http 403", edge - timedelta(minutes=10), times=3)
        self._fail(r, "http 403", edge, times=2)
        assert hiscores.recent_counts(r, edge)["fail:http 403"] == 5

    def test_bytes_from_a_raw_redis_client_are_read(self):
        r = FakeRedis()
        hiscores.record_cycle(r, "running", NOW)
        hiscores.record_health(r, hiscores.OK, "", NOW)
        r.hashes[hiscores.HEALTH_KEY] = {k.encode(): v.encode() for k, v in r.hashes[hiscores.HEALTH_KEY].items()}
        assert len(hiscores.health_checks(r, NOW)) == 4

    def _paced(self, interval, when):
        r = FakeRedis()
        hiscores.record_cycle(r, "running", when)
        hiscores.record_health(r, hiscores.OK, "", when)
        hiscores._set_interval(r, interval, "test", when)
        return r

    def test_a_slow_pace_for_hours_is_reported(self):
        """2026-11-20: every request answered, a twentieth as many, no alert."""
        r = self._paced(5000, NOW)
        later = NOW + timedelta(hours=7)
        hiscores.record_cycle(r, "running", later)
        ok, detail = self._checks(r, later)["hiscores:pace"]
        assert not ok and "5000 ms" in detail and "7 hours" in detail

    def test_a_short_slow_spell_is_not(self):
        r = self._paced(5000, NOW)
        later = NOW + timedelta(hours=3)
        hiscores.record_cycle(r, "running", later)
        assert self._checks(r, later)["hiscores:pace"][0]

    def test_the_clock_restarts_once_the_pace_recovers(self):
        r = self._paced(5000, NOW)
        hiscores._set_interval(r, 600, "test", NOW + timedelta(hours=5))
        hiscores._set_interval(r, 5000, "test", NOW + timedelta(hours=6))
        later = NOW + timedelta(hours=8)
        hiscores.record_cycle(r, "running", later)
        assert self._checks(r, later)["hiscores:pace"][0]

    def test_a_mildly_slow_pace_never_alerts(self):
        r = self._paced(hiscores.slow_pace_ms(), NOW)
        later = NOW + timedelta(days=2)
        hiscores.record_cycle(r, "running", later)
        assert self._checks(r, later)["hiscores:pace"][0]

    def test_recording_never_raises(self):
        r = FakeRedis()
        r.hset = None
        hiscores.record_health(r, hiscores.OK)
        hiscores.record_cycle(r, "running")


class TestWork:
    def test_no_more_than_the_allowed_number_in_flight(self, monkeypatch):
        r = FakeRedis()
        flight = {"now": 0, "peak": 0, "done": []}

        async def fake_poll(http, redis_conn, participant, due, now=None):
            flight["now"] += 1
            flight["peak"] = max(flight["peak"], flight["now"])
            await asyncio.sleep(0.01)
            flight["now"] -= 1
            flight["done"].append(participant["player_id"])
            return "written"

        monkeypatch.setattr(poller, "poll_player", fake_poll)
        monkeypatch.setattr(poller, "CONCURRENCY", 2)
        rotation = schedule.Due(schedule.ROTATION, False, 2)
        items = [("bg", rotation, {"player_id": i}) for i in range(9)]
        stats = {"polled": 0}
        import time as _time

        asyncio.run(poller.work(None, r, items, _time.monotonic() + 30, stats))
        assert flight["peak"] == 2 and sorted(flight["done"]) == list(range(9)) and stats["polled"] == 9
        assert stats["more"] is False

    def test_running_out_of_time_says_there_is_more(self, monkeypatch):
        async def fake_poll(http, redis_conn, participant, due, now=None):
            await asyncio.sleep(0.02)
            return "written"

        monkeypatch.setattr(poller, "poll_player", fake_poll)
        monkeypatch.setattr(poller, "CONCURRENCY", 1)
        rotation = schedule.Due(schedule.ROTATION, False, 2)
        stats = {"polled": 0}
        import time as _time

        asyncio.run(poller.work(None, FakeRedis(), [("bg", rotation, {"player_id": i}) for i in range(50)],
                                _time.monotonic() + 0.05, stats))
        assert stats["more"] is True and 0 < stats["polled"] < 50

    def test_event_participants_go_before_the_background_tier(self, monkeypatch):
        order = []

        async def fake_poll(http, redis_conn, participant, due, now=None):
            order.append(participant["player_id"])
            return "unchanged"

        monkeypatch.setattr(poller, "poll_player", fake_poll)
        monkeypatch.setattr(poller, "CONCURRENCY", 1)
        rotation = schedule.Due(schedule.ROTATION, False, 2)
        start = schedule.Due(schedule.START, True, 1)
        import time as _time

        asyncio.run(poller.work(None, FakeRedis(), [("event", start, {"player_id": 1}),
                                                    ("bg", rotation, {"player_id": 2})],
                                _time.monotonic() + 30, {"polled": 0}))
        assert order == [1, 2]

    def test_a_refusal_stops_every_worker(self, monkeypatch):
        calls = []

        async def fake_poll(http, redis_conn, participant, due, now=None):
            calls.append(participant["player_id"])
            await asyncio.sleep(0)
            return "blocked"

        monkeypatch.setattr(poller, "poll_player", fake_poll)
        monkeypatch.setattr(poller, "CONCURRENCY", 2)
        rotation = schedule.Due(schedule.ROTATION, False, 2)
        import time as _time

        stats = {"polled": 0}
        asyncio.run(poller.work(None, FakeRedis(), [("bg", rotation, {"player_id": i}) for i in range(8)],
                                _time.monotonic() + 30, stats))
        assert len(calls) <= 2 and stats["polled"] == 0


class TestWhatMayNeverFall:
    def test_rank_scores_may_fall_and_boss_counts_may_not(self, s):
        _rec(s, {"attack": 5000}, {"last_man_standing": 999, "pvp_arena": 3013, "league_points": 800,
                                   "zulrah": 50, "clue_scrolls_all": 20}, when=NOW)
        assert _rec(s, {"attack": 5100}, {"last_man_standing": 988, "pvp_arena": 2970, "league_points": 0,
                                          "zulrah": 51, "clue_scrolls_all": 20},
                    when=NOW + timedelta(hours=2)) == store.WRITTEN
        assert _rec(s, {"attack": 5200}, {"zulrah": 51, "clue_scrolls_all": 19},
                    when=NOW + timedelta(hours=4)) == store.FLAGGED

    def test_an_activity_we_do_not_know_cannot_flag_anyone(self):
        assert store.counts_only_up("zulrah") and store.counts_only_up("clue_scrolls_hard")
        assert store.counts_only_up("collections_logged")
        assert not store.counts_only_up("pvp_arena") and not store.counts_only_up("brand_new_minigame")


class TestStuckPlayersHeal:
    def test_three_consistent_refusals_over_a_day_become_the_baseline(self, s):
        _rec(s, {"attack": 9000}, {"zulrah": 80}, when=NOW)
        # The name now belongs to a smaller account.
        assert _rec(s, {"attack": 500}, {"zulrah": 3}, when=NOW + timedelta(hours=2)) == store.FLAGGED
        assert _rec(s, {"attack": 650}, {"zulrah": 4}, when=NOW + timedelta(hours=14)) == store.FLAGGED
        assert _rec(s, {"attack": 800}, {"zulrah": 5}, when=NOW + timedelta(hours=27)) == store.WRITTEN
        assert [r.flagged for r in _rows(s)] == [False, True, True, False]
        assert store.values_at(s, 7, NOW + timedelta(days=2))["taken_at"] == NOW + timedelta(hours=27)
        # And from there on, readings are judged against the new baseline.
        assert _rec(s, {"attack": 900}, {"zulrah": 5}, when=NOW + timedelta(hours=30)) == store.WRITTEN

    def test_not_before_a_day_has_passed(self, s):
        _rec(s, {"attack": 9000}, when=NOW)
        for hours, xp in ((1, 500), (2, 600), (3, 700), (4, 800)):
            assert _rec(s, {"attack": xp}, when=NOW + timedelta(hours=hours)) == store.FLAGGED

    def test_readings_that_contradict_each_other_never_heal(self, s):
        _rec(s, {"attack": 9000}, when=NOW)
        _rec(s, {"attack": 500}, when=NOW + timedelta(hours=2))
        _rec(s, {"attack": 300}, when=NOW + timedelta(hours=14))    # went down again
        assert _rec(s, {"attack": 400}, when=NOW + timedelta(hours=27)) == store.FLAGGED


class TestSessionEnds:
    """Read a player when they stop playing, the moment WOM would hear of it."""

    def test_only_a_fresh_silence_with_no_read_since(self):
        ended = schedule.session_ended
        contact = NOW - timedelta(minutes=25)
        assert ended(NOW, contact, None)
        assert ended(NOW, contact, contact - timedelta(hours=2))
        assert not ended(NOW, contact, contact + timedelta(minutes=3))     # already read since
        assert not ended(NOW, NOW - timedelta(minutes=10), None)           # still playing
        assert not ended(NOW, NOW - timedelta(hours=2), None)              # an old silence
        assert not ended(NOW, None, None)

    def test_players_who_just_stopped_are_brought_forward(self):
        r = FakeRedis()
        epoch = lambda t: (t - datetime(1970, 1, 1)).total_seconds()
        for pid in (1, 2, 3, 4):
            r.zadd(poller.QUEUE_KEY, {str(pid): epoch(NOW + timedelta(hours=5))})
        r.hset(poller._state_key(2), mapping={"polled": epoch(NOW - timedelta(minutes=5))})   # read since
        r.hset(poller._state_key(3), mapping={"polled": 1, "not_found": 1})
        contacts = {1: NOW - timedelta(minutes=25), 2: NOW - timedelta(minutes=25),
                    3: NOW - timedelta(minutes=25), 4: NOW - timedelta(minutes=5),
                    99: NOW - timedelta(minutes=25)}                                          # not queued
        assert poller.enqueue_session_ends(r, contacts, NOW) == 1
        assert r.zscore(poller.QUEUE_KEY, "1") == 2.0
        assert r.zscore(poller.QUEUE_KEY, "2") == epoch(NOW + timedelta(hours=5))
        assert r.zscore(poller.QUEUE_KEY, "4") == epoch(NOW + timedelta(hours=5))
        assert r.zscore(poller.QUEUE_KEY, "99") is None


class TestLogouts:
    """The plugin reports a logout; we read the hiscores shortly after."""

    def test_a_logout_books_a_read_ninety_seconds_out(self):
        r = FakeRedis()
        assert poller.note_logout(r, 7, now_ts=1000.0)
        assert r.zscore(poller.LOGOUT_KEY, "7") == 1000.0 + poller.LOGOUT_READ_DELAY

    def test_a_relog_loop_buys_one_read_a_minute(self):
        r = FakeRedis()
        assert poller.note_logout(r, 7, now_ts=1000.0)
        assert not poller.note_logout(r, 7, now_ts=1010.0)
        assert r.zscore(poller.LOGOUT_KEY, "7") == 1000.0 + poller.LOGOUT_READ_DELAY

    def test_redis_trouble_never_reaches_the_plugin(self):
        r = FakeRedis()
        r.broken = True
        assert poller.note_logout(r, 7) is False

    def test_due_logouts_go_to_the_very_front_and_not_before(self):
        r = FakeRedis()
        r.zadd(poller.QUEUE_KEY, {"7": 9e9, "8": 9e9})
        poller.note_logout(r, 7, now_ts=1000.0)
        poller.note_logout(r, 8, now_ts=1060.0)
        assert poller.promote_logouts(r, now_ts=1050.0) == 0
        assert poller.promote_logouts(r, now_ts=1095.0) == 1
        assert r.zscore(poller.QUEUE_KEY, "7") == 1.9 and r.zscore(poller.QUEUE_KEY, "8") == 9e9
        assert r.hgetall(poller._state_key(7))["logout"] == "1"
        assert r.zscore(poller.LOGOUT_KEY, "7") is None

    def test_a_read_that_shows_nothing_new_is_tried_once_more(self):
        r = FakeRedis()
        r.hset(poller._state_key(7), mapping={"logout": 1})
        poller.after_logout_read(r, 7, "unchanged")
        assert r.hgetall(poller._state_key(7))["logout"] == "2"
        assert r.zscore(poller.LOGOUT_KEY, "7") is not None
        # The second look settles it either way.
        poller.after_logout_read(r, 7, "unchanged")
        assert "logout" not in r.hgetall(poller._state_key(7))

    def test_a_read_with_new_numbers_settles_it(self):
        r = FakeRedis()
        r.hset(poller._state_key(7), mapping={"logout": 1})
        poller.after_logout_read(r, 7, "written")
        assert "logout" not in r.hgetall(poller._state_key(7)) and r.zscore(poller.LOGOUT_KEY, "7") is None

    def test_a_failed_read_keeps_the_logout_pending(self):
        r = FakeRedis()
        r.hset(poller._state_key(7), mapping={"logout": 1})
        poller.after_logout_read(r, 7, "error")
        assert r.hgetall(poller._state_key(7))["logout"] == "1"

    def test_an_ordinary_read_is_untouched(self):
        r = FakeRedis()
        poller.after_logout_read(r, 7, "unchanged")
        assert r.hashes.get(poller._state_key(7), {}) == {} and r.zscore(poller.LOGOUT_KEY, "7") is None
