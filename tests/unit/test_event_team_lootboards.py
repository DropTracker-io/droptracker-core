"""Unit tests for per-team event lootboards (lootboard/team_boards.py).

Everything decision-shaped lives here: path construction (the ``clans/{gid}``
root the chmod-up-to-clans logic depends on), roster resolution including the
auto_clan whole-clan fallback, window -> partition selection, and the hourly
mtime throttle. The Pillow render is exercised by running the CLI against a
real event."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from lootboard import team_boards as tb

NOW = datetime(2026, 8, 12, 12, 0, 0)


def _event(**kw):
    base = {"id": 42, "group_id": 7, "name": "Summer Bingo",
            "status": "active", "schedule_config": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _team(**kw):
    base = {"id": 3, "event_id": 42, "name": "Red", "group_id": None,
            "auto_clan": False}
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
class TestPaths:
    def test_path_keeps_the_clans_root(self):
        # _ensure_public_dir chmods every component up to (not including)
        # /clans — losing that root breaks the two-service-account write.
        path = tb.team_board_path(7, 42, 3)
        assert path == (
            "/store/droptracker/disc/static/assets/img/clans/7"
            "/events/42/teams/3/lootboard.png"
        )
        assert "/clans/" in path

    def test_dir_is_the_path_minus_the_filename(self):
        assert tb.team_board_dir(7, 42, 3) == os.path.dirname(
            tb.team_board_path(7, 42, 3))

    def test_ids_are_coerced_to_int(self):
        assert tb.team_board_path("7", "42", "3") == tb.team_board_path(7, 42, 3)

    def test_group_id_prefers_the_event_owner(self):
        assert tb.board_group_id(_event(group_id=7), _team(group_id=9)) == 7

    def test_group_id_falls_back_to_the_team_clan(self):
        assert tb.board_group_id(_event(group_id=None), _team(group_id=9)) == 9

    def test_group_id_falls_back_to_zero_for_ownerless_events(self):
        assert tb.board_group_id(_event(group_id=None), _team()) == 0

    def test_public_url(self):
        assert tb.team_board_url(tb.team_board_path(7, 42, 3)) == (
            "https://www.droptracker.io/img/clans/7/events/42/teams/3/lootboard.png"
        )

    def test_public_url_none_for_paths_outside_the_image_root(self):
        assert tb.team_board_url("/tmp/nope.png") is None


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
class TestThrottle:
    def test_missing_board_is_always_due(self, tmp_path):
        assert tb.should_regenerate(str(tmp_path / "missing.png")) is True

    def test_fresh_board_is_not_due(self, tmp_path):
        path = tmp_path / "lootboard.png"
        path.write_bytes(b"x")
        assert tb.should_regenerate(str(path)) is False

    def test_board_older_than_an_hour_is_due(self, tmp_path):
        path = tmp_path / "lootboard.png"
        path.write_bytes(b"x")
        stale = tb.REFRESH_SECONDS + 60
        os.utime(path, (os.path.getatime(path), os.path.getmtime(path) - stale))
        assert tb.should_regenerate(str(path)) is True

    def test_age_is_infinite_when_missing(self, tmp_path):
        assert tb.board_age_seconds(str(tmp_path / "nope.png")) == float("inf")

    def test_default_interval_is_hourly(self):
        assert tb.REFRESH_SECONDS == 3600


# --------------------------------------------------------------------------- #
# Feature flag
# --------------------------------------------------------------------------- #
class TestFeatureFlag:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv(tb.FEATURE_FLAG_ENV, raising=False)
        assert tb.feature_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv(tb.FEATURE_FLAG_ENV, value)
        assert tb.feature_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv(tb.FEATURE_FLAG_ENV, value)
        assert tb.feature_enabled() is False


# --------------------------------------------------------------------------- #
# Visibility gate
# --------------------------------------------------------------------------- #
class TestEventIsPublic:
    """A team board carries the event name, the team name and every member's
    RSN + GP, and lands on an unauthenticated, enumerable /img URL — so it may
    only ever be rendered for an event the whole world may already see."""

    def test_default_is_public(self):
        assert tb.event_is_public(_event()) is True

    def test_explicit_public(self):
        assert tb.event_is_public(_event(visibility="public")) is True

    def test_private_is_not_public(self):
        assert tb.event_is_public(_event(visibility="private")) is False

    def test_null_visibility_reads_as_public(self):
        # Rows written before the column existed read as public, like the web
        # gate does — not as private, which would silently blank every board.
        assert tb.event_is_public(_event(visibility=None)) is True

    def test_drafts_are_not_restricted_by_status(self):
        # Mirrors web74a: draft-ness is a listing concern, not a content one.
        # A public draft renders nothing anyway (no windows -> no partitions).
        assert tb.event_is_public(_event(status="draft")) is True
        assert tb.event_is_public(
            _event(status="draft", visibility="private")) is False

    @pytest.mark.parametrize("visibility", [None, "public", "private"])
    def test_matches_the_web_api_gate(self, visibility):
        # Anti-drift: this predicate is a copy of the events API's content
        # gate (the route module can't be imported into the lootboard
        # subprocess), so pin the two definitions together.
        import web_api.routes.events as evr

        event = _event(visibility=visibility)
        assert tb.event_is_public(event) is (not evr._is_restricted(event))


# --------------------------------------------------------------------------- #
# Roster resolution
# --------------------------------------------------------------------------- #
class TestMergeRoster:
    def test_explicit_roster_wins(self):
        assert tb.merge_roster([5, 3, 3], [99], auto_clan=True) == [3, 5]

    def test_auto_clan_falls_back_to_clan_membership(self):
        # auto_clan teams legitimately have no EventTeamMember rows — they
        # mean "every current member of group_id".
        assert tb.merge_roster([], [9, 4, 4], auto_clan=True) == [4, 9]

    def test_plain_team_with_no_roster_stays_empty(self):
        assert tb.merge_roster([], [9, 4], auto_clan=False) == []

    def test_nulls_are_dropped(self):
        assert tb.merge_roster([1, None, 2], [], auto_clan=False) == [1, 2]

    def test_ids_are_coerced_and_deduped(self):
        assert tb.merge_roster(["4", 4, 2], [], auto_clan=False) == [2, 4]


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Routes .query(col) by the identity of the stubbed model column — under
    tests/conftest.py db.models is a MagicMock, so attribute access returns a
    stable child mock."""

    def __init__(self, roster_rows, clan_rows):
        self.roster_rows = roster_rows
        self.clan_rows = clan_rows
        self.queried = []

    def query(self, column):
        from db.models import EventTeamMember

        self.queried.append(column)
        if column is EventTeamMember.player_id:
            return _FakeQuery(self.roster_rows)
        return _FakeQuery(self.clan_rows)


class TestResolveTeamPlayerIds:
    def test_explicit_roster_never_hits_the_association_table(self):
        session = _FakeSession([(11,), (12,)], [(99,)])
        assert tb.resolve_team_player_ids(session, _team()) == [11, 12]
        assert len(session.queried) == 1

    def test_auto_clan_with_no_roster_reads_clan_membership(self):
        session = _FakeSession([], [(21,), (22,)])
        team = _team(auto_clan=True, group_id=9)
        assert tb.resolve_team_player_ids(session, team) == [21, 22]
        assert len(session.queried) == 2

    def test_auto_clan_with_materialized_roster_uses_it(self):
        # The lifecycle sweep materializes auto_clan rosters; once it has, the
        # explicit rows are authoritative (a departed member was deleted).
        session = _FakeSession([(21,)], [(21,), (22,)])
        team = _team(auto_clan=True, group_id=9)
        assert tb.resolve_team_player_ids(session, team) == [21]

    def test_auto_clan_without_a_group_has_no_fallback(self):
        session = _FakeSession([], [(21,)])
        team = _team(auto_clan=True, group_id=None)
        assert tb.resolve_team_player_ids(session, team) == []

    def test_plain_empty_team_is_empty(self):
        session = _FakeSession([], [(21,)])
        assert tb.resolve_team_player_ids(session, _team()) == []


# --------------------------------------------------------------------------- #
# Window -> partitions
# --------------------------------------------------------------------------- #
class TestBoardPartitions:
    # The default retention resolves through lootboard.timeframe from the live
    # RedisLootTracker TTL, which the conftest stubs away — pin it explicitly.
    RETENTION = tb.DEFAULT_RETENTION_DAYS

    def _partitions(self, windows, **kw):
        kw.setdefault("retention_days", self.RETENTION)
        kw.setdefault("now", NOW)
        return tb.board_partitions(windows, **kw)

    def test_no_window_yields_no_partitions(self):
        assert self._partitions([]) == (tb.GRANULARITY_DAILY, [])

    def test_recent_window_uses_daily_partitions(self):
        window = (datetime(2026, 8, 10, 9, 0), datetime(2026, 8, 12, 11, 0))
        granularity, partitions = self._partitions([window])
        assert granularity == tb.GRANULARITY_DAILY
        assert partitions == ["20260810", "20260811", "20260812"]

    def test_single_day_window(self):
        window = (datetime(2026, 8, 12, 1, 0), datetime(2026, 8, 12, 11, 0))
        assert self._partitions([window])[1] == ["20260812"]

    def test_recurring_windows_are_unioned_not_bridged(self):
        # Weekend-only event: the weekdays between the windows are not part of
        # the event and must not contribute a partition.
        windows = [
            (datetime(2026, 8, 1), datetime(2026, 8, 2, 23, 59)),
            (datetime(2026, 8, 8), datetime(2026, 8, 9, 23, 59)),
        ]
        assert self._partitions(windows)[1] == [
            "20260801", "20260802", "20260808", "20260809",
        ]

    def test_window_older_than_daily_retention_falls_back_to_monthly(self):
        # Daily Redis hashes carry a 90-day TTL, so an older event can only be
        # summed at month granularity.
        window = (datetime(2026, 1, 5), datetime(2026, 2, 20))
        granularity, partitions = self._partitions([window])
        assert granularity == tb.GRANULARITY_MONTHLY
        assert partitions == ["202601", "202602"]

    def test_retention_boundary_is_configurable(self):
        window = (datetime(2026, 8, 5), datetime(2026, 8, 12))
        assert tb.board_partitions(
            [window], retention_days=3, now=NOW)[0] == tb.GRANULARITY_MONTHLY
        assert tb.board_partitions(
            [window], retention_days=30, now=NOW)[0] == tb.GRANULARITY_DAILY

    def test_inverted_windows_are_dropped(self):
        window = (datetime(2026, 8, 12), datetime(2026, 8, 10))
        assert self._partitions([window])[1] == []

    def test_none_bounds_are_dropped(self):
        assert self._partitions([(None, NOW)])[1] == []


class TestBoardTitle:
    def test_team_then_event(self):
        assert tb.board_title(_event(), _team()) == "Red | Summer Bingo"

    def test_missing_names_degrade(self):
        assert tb.board_title(_event(name=None), _team(name=None)) == (
            "Team | Event")


# --------------------------------------------------------------------------- #
# Render gate
# --------------------------------------------------------------------------- #
class _ExplodingSession:
    """Any DB access at all means the gate let a render start."""

    def query(self, *args, **kwargs):
        raise AssertionError("gated render must not touch the DB")


class TestRenderGate:
    """render_team_board re-checks the flag and the visibility gate itself:
    the CLI calls it directly, so the sweep's checks are not enough."""

    @pytest.fixture
    def reached(self, monkeypatch):
        """Records the events whose render got past the gate, and stops each
        one at the next step (no windows -> no partitions -> no image)."""
        seen = []

        def _windows(session, event, now=None):
            seen.append(event)
            return []

        monkeypatch.setattr(tb, "event_windows", _windows)
        return seen

    @pytest.mark.asyncio
    async def test_public_event_renders_when_enabled(self, monkeypatch, reached):
        monkeypatch.setenv(tb.FEATURE_FLAG_ENV, "1")
        assert await tb.render_team_board(
            _ExplodingSession(), _event(), _team(), force=True) is None
        assert len(reached) == 1

    @pytest.mark.asyncio
    async def test_private_event_is_refused(self, monkeypatch, reached):
        monkeypatch.setenv(tb.FEATURE_FLAG_ENV, "1")
        assert await tb.render_team_board(
            _ExplodingSession(), _event(visibility="private"), _team(),
            force=True) is None
        assert reached == []

    @pytest.mark.asyncio
    async def test_force_does_not_bypass_visibility(self, monkeypatch, reached):
        monkeypatch.setenv(tb.FEATURE_FLAG_ENV, "1")
        event = _event(visibility="private")
        for force in (True, False):
            assert await tb.render_team_board(
                _ExplodingSession(), event, _team(), force=force) is None
        assert reached == []

    @pytest.mark.asyncio
    async def test_force_does_not_bypass_the_feature_flag(self, monkeypatch,
                                                          reached):
        monkeypatch.delenv(tb.FEATURE_FLAG_ENV, raising=False)
        assert await tb.render_team_board(
            _ExplodingSession(), _event(), _team(), force=True) is None
        assert reached == []


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
class _TargetSession:
    def __init__(self, events, teams):
        self._events = events
        self._teams = teams
        self.filters = []

    def query(self, model):
        from db.models import Event

        rows = self._events if model is Event else self._teams
        return _TargetQuery(rows)

    def close(self):
        pass


class _TargetQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class TestCollectTargets:
    def test_returns_ids_most_stale_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        event = _event()
        teams = [_team(id=1), _team(id=2)]
        # Team 1 has a board written 2h ago; team 2 has none (age = inf).
        path1 = tb.team_board_path(7, 42, 1)
        os.makedirs(os.path.dirname(path1), exist_ok=True)
        with open(path1, "wb") as fh:
            fh.write(b"x")
        os.utime(path1, (os.path.getatime(path1),
                         os.path.getmtime(path1) - 7200))

        session = _TargetSession([event], teams)
        targets = tb.collect_targets(session)
        assert [(eid, tid) for eid, tid, _, _ in targets] == [(42, 2), (42, 1)]

    def test_fresh_boards_are_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        path = tb.team_board_path(7, 42, 1)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")

        session = _TargetSession([_event()], [_team(id=1)])
        assert tb.collect_targets(session) == []

    def test_force_ignores_the_throttle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        path = tb.team_board_path(7, 42, 1)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")

        session = _TargetSession([_event()], [_team(id=1)])
        assert len(tb.collect_targets(session, force=True)) == 1

    def test_no_events_means_no_targets(self):
        assert tb.collect_targets(_TargetSession([], [_team()])) == []

    def test_orphan_team_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        session = _TargetSession([_event(id=42)], [_team(id=1, event_id=99)])
        assert tb.collect_targets(session) == []

    def test_private_event_is_never_collected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        session = _TargetSession([_event(visibility="private")],
                                 [_team(id=1)])
        assert tb.collect_targets(session) == []

    def test_force_does_not_collect_a_private_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        session = _TargetSession([_event(visibility="private")],
                                 [_team(id=1)])
        assert tb.collect_targets(session, force=True) == []

    def test_naming_a_private_event_by_id_does_not_collect_it(
            self, tmp_path, monkeypatch):
        # The explicit-id branch (what the CLI's --event uses) is filtered too:
        # asking for one event by id is not consent to publish it.
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        session = _TargetSession([_event(visibility="private")],
                                 [_team(id=1)])
        assert tb.collect_targets(session, event_id=42, force=True) == []

    def test_public_event_is_still_collected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        session = _TargetSession([_event(visibility="public")], [_team(id=1)])
        assert [(eid, tid) for eid, tid, _, _ in
                tb.collect_targets(session)] == [(42, 1)]

    def test_only_the_private_events_teams_are_dropped(self, tmp_path,
                                                       monkeypatch):
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))
        session = _TargetSession(
            [_event(id=42, visibility="private"), _event(id=43)],
            [_team(id=1, event_id=42), _team(id=2, event_id=43)])
        assert [(eid, tid) for eid, tid, _, _ in
                tb.collect_targets(session)] == [(43, 2)]


class TestSweep:
    @pytest.mark.asyncio
    async def test_sweep_is_a_noop_while_the_flag_is_off(self, monkeypatch):
        monkeypatch.delenv(tb.FEATURE_FLAG_ENV, raising=False)

        def _boom():
            raise AssertionError("sweep must not touch the DB while disabled")

        assert await tb.sweep_team_boards(session_factory=_boom) == []

    @pytest.mark.asyncio
    async def test_force_does_not_bypass_the_feature_flag(self, monkeypatch):
        # The CLI forces every run; force means "ignore the hourly throttle",
        # never "publish boards on a deployment that has the feature off".
        monkeypatch.delenv(tb.FEATURE_FLAG_ENV, raising=False)

        def _boom():
            raise AssertionError("sweep must not touch the DB while disabled")

        assert await tb.sweep_team_boards(
            session_factory=_boom, event_id=42, force=True) == []

    @pytest.mark.asyncio
    async def test_enabled_sweep_writes_nothing_for_a_private_event(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv(tb.FEATURE_FLAG_ENV, "1")
        monkeypatch.setattr(tb, "IMG_ROOT", str(tmp_path))

        async def _boom(*args, **kwargs):
            raise AssertionError("private events must not reach the renderer")

        monkeypatch.setattr(tb, "render_team_board", _boom)
        session = _TargetSession([_event(visibility="private")], [_team(id=1)])
        assert await tb.sweep_team_boards(
            session_factory=lambda: session, force=True) == []
