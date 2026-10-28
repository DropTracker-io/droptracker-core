"""Unit tests for the pure-decision parts of services/event_signup.py.

Loaded directly from the file path (like test_event_lifecycle_sweep) so the
conftest db/services stubs never interfere — the module's top-level imports are
stdlib-only by design (DB models are lazy-imported inside functions). We drive
the mode/guard branches that need no DB, using tiny fakes.
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_signup.py",
)
_spec = importlib.util.spec_from_file_location("_event_signup_under_test", _PATH)
sus = importlib.util.module_from_spec(_spec)
sys.modules["_event_signup_under_test"] = sus
_spec.loader.exec_module(sus)


def _ev(**kw):
    # allow_late_signups on by default so these fixtures exercise the guard
    # under test rather than web70a's window (a started event closes sign-ups
    # otherwise); TestSignupWindow covers the window itself.
    base = dict(id=1, mode="standard", formation_mode="self_join", status="active",
                group_id=5, starts_at=None, ends_at=None, join_code=None,
                activated_at=None, allow_late_signups=True)
    base.update(kw)
    return SimpleNamespace(**base)


class TestSelfSignupMode:
    def test_signup_modes(self, monkeypatch):
        monkeypatch.setattr(sus, "EVENT_SELF_SIGNUP_MODES",
                            ("self_join", "auto_assign", "signup_pool"), raising=False)
        # Patch the lazy import target used inside is_self_signup_mode.
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")
        monkeypatch.setitem(sys.modules, "db.models", fake)
        for m in ("self_join", "auto_assign", "signup_pool"):
            assert sus.is_self_signup_mode(_ev(formation_mode=m))
        assert not sus.is_self_signup_mode(_ev(formation_mode="admin_assign"))


class TestPerformSignupGuards:
    """Guard branches that raise before any DB query."""

    def _modes(self, monkeypatch):
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")
        monkeypatch.setitem(sys.modules, "db.models", fake)

    def test_past_event_rejected(self, monkeypatch):
        self._modes(monkeypatch)
        player = SimpleNamespace(player_id=3)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(status="past"), player, 7)
        assert exc.value.status == 409

    def test_ended_window_rejected(self, monkeypatch):
        self._modes(monkeypatch)
        player = SimpleNamespace(player_id=3)
        ev = _ev(ends_at=datetime.now() - timedelta(hours=1))
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, ev, player, 7)
        assert exc.value.status == 409

    def test_admin_assign_rejected(self, monkeypatch):
        self._modes(monkeypatch)
        player = SimpleNamespace(player_id=3)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(formation_mode="admin_assign"), player, 7)
        assert exc.value.status == 403


class TestSignupWindow:
    """web70a: self sign-ups close when the event begins unless the event opted
    into late sign-ups. This is what retires the Discord prompt's button."""

    def _modes(self, monkeypatch):
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")
        monkeypatch.setitem(sys.modules, "db.models", fake)

    def test_scheduled_event_before_start_is_open(self):
        ev = _ev(status="draft", allow_late_signups=False,
                 starts_at=datetime.now() + timedelta(days=1))
        assert sus.signups_closed(ev) is None
        assert not sus.event_started(ev)

    def test_active_event_closes_by_default(self):
        ev = _ev(status="active", allow_late_signups=False)
        assert sus.signups_closed(ev) == "Sign-ups closed when the event began."

    def test_draft_past_its_start_time_closes(self):
        # The lifecycle sweep is about to activate it; don't take entries in
        # the gap.
        ev = _ev(status="draft", allow_late_signups=False,
                 starts_at=datetime.now() - timedelta(minutes=5))
        assert sus.signups_closed(ev) is not None

    def test_late_signups_keeps_a_running_event_open(self):
        ev = _ev(status="active", allow_late_signups=True,
                 ends_at=datetime.now() + timedelta(days=1))
        assert sus.signups_closed(ev) is None

    def test_late_signups_still_closes_at_the_end(self):
        ev = _ev(status="active", allow_late_signups=True,
                 ends_at=datetime.now() - timedelta(minutes=1))
        assert sus.signups_closed(ev) == "This event is over — sign-ups are closed."

    def test_close_time_is_the_start_by_default(self):
        starts = datetime.now() + timedelta(hours=2)
        ends = starts + timedelta(days=3)
        assert sus.signup_close_at(
            _ev(allow_late_signups=False, starts_at=starts, ends_at=ends)) == starts
        # Activation wins over the schedule (an admin may start it early).
        activated = datetime.now()
        assert sus.signup_close_at(
            _ev(allow_late_signups=False, starts_at=starts, ends_at=ends,
                activated_at=activated)) == activated

    def test_close_time_is_the_end_with_late_signups(self):
        ends = datetime.now() + timedelta(days=3)
        assert sus.signup_close_at(
            _ev(allow_late_signups=True, starts_at=datetime.now(), ends_at=ends)) == ends

    def test_perform_signup_refuses_a_started_event(self, monkeypatch):
        self._modes(monkeypatch)
        player = SimpleNamespace(player_id=3)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(status="active", allow_late_signups=False),
                               player, 7)
        assert exc.value.status == 409
        assert "began" in exc.value.detail


class TestMultiClanGuard:
    """G7: a player in more than one participating clan can't self/auto sign up."""

    def test_standard_event_is_noop_no_db(self):
        # Standard events short-circuit before any membership query (session=None
        # would blow up if it were touched).
        sus.assert_single_participating_clan(None, _ev(mode="standard"), 3)
        assert sus.participating_clans_for_player(None, _ev(mode="standard"), 3) == set()

    def test_intersection_is_the_players_participating_clans(self, monkeypatch):
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {20, 99})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20})
        assert sus.participating_clans_for_player(
            None, _ev(mode="clan_vs_clan"), 3
        ) == {20}

    def test_single_clan_is_allowed(self, monkeypatch):
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {20})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20})
        sus.assert_single_participating_clan(None, _ev(mode="clan_vs_clan"), 3)  # no raise

    def test_multi_clan_blocks_409(self, monkeypatch):
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {10, 20})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20, 30})
        with pytest.raises(sus.SignupError) as exc:
            sus.assert_single_participating_clan(None, _ev(mode="clan_vs_clan"), 3)
        assert exc.value.status == 409

    def test_signup_group_picks_the_single_clan(self, monkeypatch):
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {20, 99})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20})
        assert sus.signup_group_for_player(None, _ev(mode="clan_vs_clan"), 3) == 20

    def test_perform_signup_blocks_multi_clan_before_recording(self, monkeypatch):
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")
        monkeypatch.setitem(sys.modules, "db.models", fake)
        # Get past eligibility so the multi-clan guard is what fires.
        monkeypatch.setattr(sus, "assert_player_eligible", lambda s, ev, pid: None)
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {10, 20})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20})
        player = SimpleNamespace(player_id=3)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(mode="clan_vs_clan"), player, 7)
        assert exc.value.status == 409
        assert "clan" in exc.value.detail  # the multi-clan guard, not the window


class TestSignupError:
    def test_carries_problem_fields(self):
        e = sus.SignupError(422, "Nope", "because")
        assert (e.status, e.title, e.detail) == (422, "Nope", "because")
        assert str(e) == "because"


# ── signup_all_members: real queries against an in-memory SQLite schema ─────

class TestPickOnePerUser:
    def test_keeps_newest_account_per_user_and_every_unclaimed(self):
        old, new = datetime(2026, 1, 1), datetime(2026, 9, 1)
        rows = [(1, 7, old), (2, 7, new), (3, None, None), (4, None, old), (5, 8, None)]
        kept, alts = sus._pick_one_per_user(rows)
        assert kept == [2, 3, 4, 5]
        assert alts == 1

    def test_tie_prefers_lowest_player_id(self):
        same = datetime(2026, 5, 5)
        assert sus._pick_one_per_user([(9, 1, same), (4, 1, same)]) == ([4], 1)


@pytest.fixture
def mini_db(monkeypatch):
    """Just the tables signup_all_members touches, as a stand-in db.models."""
    import types

    from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, String,
                            Table, create_engine)
    from sqlalchemy.orm import declarative_base, sessionmaker

    Base = declarative_base()

    class Player(Base):
        __tablename__ = "players"
        player_id = Column(Integer, primary_key=True)
        user_id = Column(Integer)
        date_updated = Column(DateTime)

    uga = Table("user_group_association", Base.metadata,
                Column("id", Integer, primary_key=True),
                Column("player_id", Integer), Column("user_id", Integer),
                Column("group_id", Integer))

    class EventTeam(Base):
        __tablename__ = "teams"
        id = Column(Integer, primary_key=True)
        event_id = Column(Integer)
        group_id = Column(Integer)
        auto_clan = Column(Boolean, default=False)

    class EventTeamMember(Base):
        __tablename__ = "members"
        team_id = Column(Integer, ForeignKey("teams.id"), primary_key=True)
        player_id = Column(Integer, primary_key=True)
        event_id = Column(Integer)

    class EventSignup(Base):
        __tablename__ = "signups"
        id = Column(Integer, primary_key=True)
        event_id = Column(Integer)
        player_id = Column(Integer)
        group_id = Column(Integer)
        user_id = Column(Integer)
        source = Column(String(16))

    class EventGroup(Base):
        __tablename__ = "event_groups"
        id = Column(Integer, primary_key=True)
        event_id = Column(Integer)
        group_id = Column(Integer)
        status = Column(String(16))

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    fake = types.ModuleType("db.models")
    for name, obj in dict(Player=Player, user_group_association=uga, EventTeam=EventTeam,
                          EventTeamMember=EventTeamMember, EventSignup=EventSignup,
                          EventGroup=EventGroup).items():
        setattr(fake, name, obj)
    monkeypatch.setitem(sys.modules, "db.models", fake)
    monkeypatch.setattr(sus, "sync_buyin_teams", lambda *a, **k: None)
    s = sessionmaker(bind=engine)()

    def member(pid, gid, uid=None, updated=None):
        if s.get(Player, pid) is None:
            s.add(Player(player_id=pid, user_id=uid, date_updated=updated))
        s.execute(uga.insert().values(player_id=pid, user_id=uid, group_id=gid))

    yield SimpleNamespace(s=s, m=fake, member=member)
    s.close()


class TestSignupAllMembers:
    def test_pool_mode_signs_everyone_up_without_placing(self, mini_db):
        s, m = mini_db.s, mini_db.m
        for pid in (1, 2, 3):
            mini_db.member(pid, 5)
        mini_db.member(4, 99)                      # another clan: not ours
        s.add(m.EventTeam(id=1, event_id=1, group_id=5))
        s.commit()
        res = sus.signup_all_members(s, _ev(formation_mode="signup_pool"))
        s.commit()
        assert (res["added"], res["placed"], res["pooled"]) == (3, 0, True)
        rows = s.query(m.EventSignup).all()
        assert sorted(r.player_id for r in rows) == [1, 2, 3]
        assert {(r.group_id, r.source) for r in rows} == {(5, "bulk")}
        assert s.query(m.EventTeamMember).count() == 0

    def test_only_adds_and_skips_alts_of_entered_users(self, mini_db):
        s, m = mini_db.s, mini_db.m
        old, new = datetime(2026, 1, 1), datetime(2026, 9, 1)
        mini_db.member(1, 5, uid=70)               # already on a team
        mini_db.member(2, 5, uid=70)               # alt of an entered user
        mini_db.member(3, 5, uid=80, updated=old)  # two accounts, none entered:
        mini_db.member(4, 5, uid=80, updated=new)  #   the newest one goes in
        mini_db.member(5, 5)                       # already signed up
        mini_db.member(6, 5)
        s.add_all([m.EventTeam(id=1, event_id=1, group_id=5),
                   m.EventTeam(id=2, event_id=1, group_id=5)])
        s.add(m.EventTeamMember(team_id=1, player_id=1, event_id=1))
        s.add(m.EventSignup(event_id=1, player_id=5, group_id=5, source="web"))
        s.commit()
        res = sus.signup_all_members(s, _ev(formation_mode="auto_assign"))
        s.commit()
        assert res["already"] == 2
        assert res["skipped_alts"] == 2            # player 2 + player 3
        assert res["added"] == 2 and res["placed"] == 2
        placed = dict(s.query(m.EventTeamMember.player_id, m.EventTeamMember.team_id).all())
        # Balanced: team 1 already had one member, so 4 → team 2, 6 → team 1.
        assert placed == {1: 1, 4: 2, 6: 1}
        # A second run is a no-op.
        again = sus.signup_all_members(s, _ev(formation_mode="auto_assign"))
        assert again["added"] == 0

    def test_clan_vs_clan_routes_by_clan_and_skips_multi_clan(self, mini_db):
        s, m = mini_db.s, mini_db.m
        mini_db.member(1, 10)
        mini_db.member(2, 20)
        mini_db.member(3, 10)
        mini_db.member(3, 20)                      # in both: G7, skipped
        s.add_all([m.EventGroup(event_id=1, group_id=10, status="accepted"),
                   m.EventGroup(event_id=1, group_id=20, status="accepted"),
                   m.EventTeam(id=1, event_id=1, group_id=10),
                   m.EventTeam(id=2, event_id=1, group_id=20)])
        s.commit()
        ev = _ev(mode="clan_vs_clan", formation_mode="admin_assign", group_id=None)
        res = sus.signup_all_members(s, ev, group_id=10)
        s.commit()
        assert (res["added"], res["skipped_multi_clan"]) == (1, 1)
        assert dict(s.query(m.EventTeamMember.player_id, m.EventTeamMember.team_id).all()) \
            == {1: 1}
        res = sus.signup_all_members(s, ev)        # the rest: clan 20
        assert res["added"] == 1
        with pytest.raises(sus.SignupError) as exc:
            sus.signup_all_members(s, ev, group_id=30)
        assert exc.value.status == 422

    def test_whole_clan_team_and_global_events_are_refused(self, mini_db):
        s, m = mini_db.s, mini_db.m
        mini_db.member(1, 5)
        s.add(m.EventTeam(id=1, event_id=1, group_id=5, auto_clan=True))
        s.commit()
        with pytest.raises(sus.SignupError) as exc:
            sus.signup_all_members(s, _ev(formation_mode="admin_assign"))
        assert exc.value.title == "Already the whole clan"
        with pytest.raises(sus.SignupError) as exc:
            sus.signup_all_members(s, _ev(group_id=None))
        assert exc.value.status == 409

    def test_placing_modes_need_a_team(self, mini_db):
        mini_db.member(1, 5)
        mini_db.s.commit()
        with pytest.raises(sus.SignupError) as exc:
            sus.signup_all_members(mini_db.s, _ev(formation_mode="self_join"))
        assert exc.value.title == "No teams"


class TestSolo:
    """Free-for-all (formation_mode="solo"): the pure halves."""

    def _models(self, monkeypatch):
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool", "solo")
        fake.EVENT_SOLO_BLOCKED_KINDS = ("conquest", "sotw", "botw")
        fake.EVENT_SOLO_BLOCKED_MODES = ("clan_vs_clan",)
        monkeypatch.setitem(sys.modules, "db.models", fake)

    def test_solo_is_a_self_signup_mode(self, monkeypatch):
        self._models(monkeypatch)
        assert sus.is_self_signup_mode(_ev(formation_mode="solo"))
        assert sus.is_solo(_ev(formation_mode="solo"))
        assert not sus.is_solo(_ev(formation_mode="self_join"))
        assert not sus.is_solo(_ev(formation_mode=None))

    def test_blocked_kinds_and_modes(self, monkeypatch):
        self._models(monkeypatch)
        for kind in ("standard", "bingo", "board_game", "loot_sweep"):
            assert sus.solo_blocked_reason(kind, "standard") is None
        assert sus.solo_blocked_reason(None, None) is None
        for kind in ("conquest", "sotw", "botw"):
            assert sus.solo_blocked_reason(kind, "standard")
        assert sus.solo_blocked_reason("standard", "clan_vs_clan")

    def test_team_name_is_the_rsn(self):
        taken = set()
        assert sus.solo_team_name("Zezima", 1, taken) == "Zezima"
        assert "zezima" in taken

    def test_team_name_dedupes_case_insensitively(self):
        taken = {"zezima", "zezima (2)"}
        assert sus.solo_team_name("ZEZIMA", 2, taken) == "ZEZIMA (3)"

    def test_team_name_falls_back_to_id(self):
        assert sus.solo_team_name(None, 42, set()) == "Player 42"
        assert sus.solo_team_name("   ", 42, set()) == "Player 42"

    def test_team_name_fits_the_column(self):
        name = sus.solo_team_name("x" * 200, 1, {"x" * 72})
        assert len(name) <= 80

    def test_split_plan(self):
        plan = sus.plan_solo_split([(10, [3, 1, 2]), (11, [4]), (12, [])])
        assert plan == {"split": {10: [1, 2, 3]}, "kept": [11], "empty": [12]}

    def test_split_plan_nothing_to_do(self):
        plan = sus.plan_solo_split([(11, [4]), (13, [5])])
        assert plan["split"] == {}
        assert plan["kept"] == [11, 13]

    def test_perform_signup_admin_assign_still_refused(self, monkeypatch):
        self._models(monkeypatch)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(formation_mode="admin_assign"),
                               SimpleNamespace(player_id=3), 7)
        assert exc.value.status == 403


class TestClanSyncRemovals:
    """web152a: the leave half of the clan roster sync, and its guard."""

    def test_departed_entrants_are_removed(self):
        remove, held = sus.plan_clan_sync_removals({1, 2, 3, 4}, {1, 2, 3, 9})
        assert remove == {4} and held is False

    def test_nothing_to_remove(self):
        assert sus.plan_clan_sync_removals({1, 2}, {1, 2, 3}) == (set(), False)
        assert sus.plan_clan_sync_removals(set(), set()) == (set(), False)

    def test_an_empty_membership_read_never_empties_the_event(self):
        assert sus.plan_clan_sync_removals({1, 2, 3}, set()) == (set(), True)

    def test_most_of_the_roster_leaving_at_once_is_held(self):
        entrants = set(range(20))
        members = set(range(5))          # 15 of 20 "gone"
        assert sus.plan_clan_sync_removals(entrants, members) == (set(), True)

    def test_a_small_roster_can_lose_a_majority(self):
        # Under the floor, a real exodus from a tiny event still applies.
        remove, held = sus.plan_clan_sync_removals({1, 2, 3}, {1, 7})
        assert remove == {2, 3} and held is False
