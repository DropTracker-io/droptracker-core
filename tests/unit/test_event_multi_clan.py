"""Multi-clan-membership exclusion (G7).

The shared pure helper ``event_engine.multi_clan_players`` decides who is left
off the whole-clan auto teams: a player in more than one clan competing in the
same event. Loaded by file path so the conftest db stubs never interfere (the
helper is pure; event_engine's top-level imports are stdlib + sqlalchemy).
"""
import importlib.util
import os
import sys
import time

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_mc", _PATH)
ee = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_mc"] = ee
_spec.loader.exec_module(ee)


class TestMultiClanPlayers:
    def test_empty(self):
        assert ee.multi_clan_players({}, []) == set()

    def test_single_clan_membership_is_never_excluded(self):
        m = {10: {1, 2, 3}, 20: {4, 5}}
        assert ee.multi_clan_players(m, [10, 20]) == set()

    def test_overlap_is_excluded(self):
        m = {10: {1, 2, 3}, 20: {3, 4}, 30: {3, 5}}
        assert ee.multi_clan_players(m, [10, 20, 30]) == {3}

    def test_only_considers_requested_gids(self):
        m = {10: {1, 2}, 20: {2, 3}, 30: {2}}
        # Restrict to {10, 20}: player 2 is in both -> excluded; clan 30 ignored,
        # so the fact that 2 is also there does not add a third count.
        assert ee.multi_clan_players(m, [10, 20]) == {2}

    def test_duplicate_rows_within_one_clan_do_not_count_twice(self):
        # The NULL-user_id insert race can duplicate a membership row; a doubled
        # row in ONE clan must not read as membership in two clans.
        m = {10: [1, 1, 2], 20: [3]}
        assert ee.multi_clan_players(m, [10, 20]) == set()

    def test_scale_12_clans_500_members(self):
        clans = {}
        pid = 0
        for gid in range(12):
            clans[gid] = set(range(pid, pid + 500))
            pid += 500
        # 40 players hold membership in exactly two clans.
        shared = set(range(1_000_000, 1_000_040))
        clans[0] |= shared
        clans[1] |= shared
        start = time.perf_counter()
        excluded = ee.multi_clan_players(clans, clans.keys())
        elapsed = time.perf_counter() - start
        assert excluded == shared
        # ~6k memberships is trivial work — a generous ceiling guards against an
        # accidental O(n^2) regression, not a real perf budget.
        assert elapsed < 0.5
