"""A clan's recap rank must not count the system groups.

`gleaderboard:{partition}` carries group 2 ("DropTracker.io"), which holds every
tracked player, so its score is an order of magnitude above any real clan and it
sits at the top of the set permanently. `_group_rank` used a bare `zrevrank`,
which counted it: for 2026-08 the top clan's own card read "#2 of 284" while the
site's leaderboard — which has always dropped these ids — called it #1. Because
group 2 outscores everyone, the error was every clan, every month, off by one.

Loaded from the file path so the conftest `services` stub doesn't shadow it.
"""

import importlib.util
import os
import sys

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "recap.py",
)
_spec = importlib.util.spec_from_file_location("_recap_rank_under_test", _MODULE_PATH)
recap = importlib.util.module_from_spec(_spec)
sys.modules["_recap_rank_under_test"] = recap
_spec.loader.exec_module(recap)


class _FakeZSet:
    """A sorted set with Redis's zrevrank/zscore/zcard semantics."""

    def __init__(self, scores):
        self._scores = dict(scores)
        self._order = [g for g, _ in sorted(scores.items(), key=lambda kv: -kv[1])]

    def zrevrank(self, _key, member):
        member = int(member)
        return self._order.index(member) if member in self._order else None

    def zscore(self, _key, member):
        return self._scores.get(int(member))

    def zcard(self, _key):
        return len(self._order)


def _wire(monkeypatch, scores):
    import types
    conn = _FakeZSet(scores)
    monkeypatch.setitem(
        sys.modules, "web_api.common",
        types.SimpleNamespace(
            _rc=lambda: conn,
            group_totals_key=lambda partition: f"gleaderboard:{partition}",
        ),
    )
    return conn


# The real August 2026 shape: group 2 far above the field.
_AUGUST = {2: 458_688_593_279, 227: 26_914_209_872, 190: 23_941_772_312,
           270: 13_601_965_892, 72: 13_215_561_753}


class TestSystemGroupsAreNotCompetitors:
    def test_top_clan_is_rank_one_not_two(self, monkeypatch):
        _wire(monkeypatch, _AUGUST)
        rank, of, score = recap._group_rank(227, 202608)
        assert rank == 1
        assert score == 26_914_209_872

    def test_the_field_size_drops_the_system_group(self, monkeypatch):
        _wire(monkeypatch, _AUGUST)
        assert recap._group_rank(227, 202608)[1] == len(_AUGUST) - 1

    def test_every_clan_below_it_shifts_up_by_one(self, monkeypatch):
        _wire(monkeypatch, _AUGUST)
        assert [recap._group_rank(g, 202608)[0] for g in (227, 190, 270, 72)] == [1, 2, 3, 4]

    def test_board_score_is_still_the_group_s_own(self, monkeypatch):
        # The rank moves; the number the card shows its provenance with must not.
        _wire(monkeypatch, _AUGUST)
        assert recap._group_rank(190, 202608)[2] == 23_941_772_312

    def test_all_three_system_ids_are_excluded(self, monkeypatch):
        _wire(monkeypatch, {0: 9 * 10**11, 1: 8 * 10**11, 2: 7 * 10**11,
                            227: 10**9, 190: 10**8})
        assert recap._group_rank(227, 202608) == (1, 2, 10**9)
        assert recap._group_rank(190, 202608)[0] == 2

    def test_a_system_group_below_a_clan_does_not_shift_it(self, monkeypatch):
        # Only system groups ranked ABOVE the subject are discounted.
        _wire(monkeypatch, {227: 10**9, 2: 10**3})
        rank, of, _ = recap._group_rank(227, 202608)
        assert rank == 1 and of == 1

    def test_no_system_groups_present_leaves_ranks_untouched(self, monkeypatch):
        _wire(monkeypatch, {227: 10**9, 190: 10**8})
        assert recap._group_rank(190, 202608) == (2, 2, 10**8)

    def test_a_system_group_asking_about_itself_is_not_self_excluded(self, monkeypatch):
        # Not a real recap subject, but the arithmetic must not go negative.
        _wire(monkeypatch, _AUGUST)
        rank, of, _ = recap._group_rank(2, 202608)
        assert rank == 1 and of == len(_AUGUST)

    def test_a_group_absent_from_the_board_has_no_rank(self, monkeypatch):
        # ~11 of 161 eligible groups legitimately have none.
        _wire(monkeypatch, _AUGUST)
        assert recap._group_rank(9999, 202608) == (None, None, None)

    def test_redis_being_unavailable_is_not_an_error(self, monkeypatch):
        import types
        monkeypatch.setitem(
            sys.modules, "web_api.common",
            types.SimpleNamespace(
                _rc=lambda: None,
                group_totals_key=lambda partition: f"gleaderboard:{partition}",
            ),
        )
        assert recap._group_rank(227, 202608) == (None, None, None)
