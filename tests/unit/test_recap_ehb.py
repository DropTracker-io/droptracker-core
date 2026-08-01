"""EHB harvesting for recap cards (services/recap_ehb.py).

Two things are worth pinning down here, and neither is arithmetic.

*Attribution.* A WOM clan roster is not our roster. Rows we cannot place must be
dropped rather than guessed at, because a clan total that counted strangers
would not match the membership every other number on the card is scoped to.

*Restraint.* WOM asked us to reduce our call volume, so the harvest is built to
ask once and remember: a clan already fetched is not fetched again, a fetch that
failed is left to the next run, and both caps report what they dropped instead
of quietly shrinking the coverage.

Loaded from the file path so the conftest ``services`` stub doesn't shadow it.
"""

import asyncio
import importlib.util
import os
import sys

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "recap_ehb.py",
)
_spec = importlib.util.spec_from_file_location("_recap_ehb_under_test", _MODULE_PATH)
ehb = importlib.util.module_from_spec(_spec)
sys.modules["_recap_ehb_under_test"] = ehb
_spec.loader.exec_module(ehb)


def _row(wom_id=None, name=None, gained=1.5, *, metric="ehb"):
    """One `bulk-gained` row, trimmed to the keys the harvest reads."""
    return {
        "player": {"id": wom_id, "displayName": name},
        "data": [
            {"metric": "overall", "gained": 12345},
            {"metric": metric, "gained": gained},
        ],
    }


class TestExtractEhb:
    def test_finds_the_metric_among_all_the_others(self):
        assert ehb._extract_ehb(_row(gained=0.789)) == 0.789

    def test_absent_metric_is_none_not_zero(self):
        # "WOM didn't report it" and "they bossed nothing" are different facts
        # and the card treats them differently.
        assert ehb._extract_ehb(_row(metric="ehp")) is None

    def test_a_measured_zero_survives(self):
        assert ehb._extract_ehb(_row(gained=0)) == 0.0

    def test_negative_gains_clamp_to_zero(self):
        # EHB only accumulates in play, so a negative is a hiscore rollback or a
        # reused name. "Gained -2.3 hours" is not a thing a card can say.
        assert ehb._extract_ehb(_row(gained=-2.3)) == 0.0

    def test_malformed_rows_are_none(self):
        for bad in (None, [], "nope", {}, {"data": None}, {"data": "x"},
                    {"data": [{"metric": "ehb", "gained": "many"}]},
                    {"data": [{"metric": "ehb"}]}):
            assert ehb._extract_ehb(bad) is None


class TestMatchRows:
    def test_matches_on_wom_id(self):
        out = ehb.match_rows([_row(wom_id=2188996, gained=3.5)], {2188996: 795}, {})
        assert out == {795: 3.5}

    def test_falls_back_to_the_normalised_display_name(self):
        # Plenty of our rows predate ever learning a WOM id, and WOM hands back
        # hyphens/underscores as spaces.
        out = ehb.match_rows(
            [_row(wom_id=None, name="Btw_Fe-male", gained=2.0)],
            {}, {"btw fe male": 42},
        )
        assert out == {42: 2.0}

    def test_wom_id_wins_over_the_name(self):
        out = ehb.match_rows(
            [_row(wom_id=7, name="Someone Else", gained=1.0)],
            {7: 111}, {"someone else": 222},
        )
        assert out == {111: 1.0}

    def test_unmatched_rows_are_dropped(self):
        # A WOM roster routinely holds accounts DropTracker doesn't track.
        assert ehb.match_rows([_row(wom_id=999, name="Stranger")], {1: 2}, {}) == {}

    def test_rows_without_the_metric_are_dropped_not_zeroed(self):
        assert ehb.match_rows([_row(wom_id=7, metric="ehp")], {7: 111}, {}) == {}

    def test_junk_rows_do_not_derail_the_batch(self):
        out = ehb.match_rows(
            [None, "x", {"player": "nope"}, _row(wom_id=7, gained=4.0)],
            {7: 111}, {},
        )
        assert out == {111: 4.0}

    def test_unusable_wom_id_falls_through_to_the_name(self):
        out = ehb.match_rows(
            [_row(wom_id="not-a-number", name="Buzzyn", gained=1.25)],
            {7: 111}, {"buzzyn": 795},
        )
        assert out == {795: 1.25}


class _FakeRedis:
    """Just the two calls the attempt marker uses."""

    def __init__(self, blow_up=False):
        self.store: dict = {}
        self.blow_up = blow_up

    def get(self, key):
        if self.blow_up:
            raise RuntimeError("redis down")
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self.blow_up:
            raise RuntimeError("redis down")
        self.store[key] = value


class TestAttemptMarker:
    def test_round_trips(self):
        conn = _FakeRedis()
        assert not ehb._attempted(conn, "g", 14, "2026-07")
        ehb._mark_attempted(conn, "g", 14, "2026-07")
        assert ehb._attempted(conn, "g", 14, "2026-07")

    def test_groups_and_players_do_not_collide(self):
        conn = _FakeRedis()
        ehb._mark_attempted(conn, "g", 14, "2026-07")
        assert not ehb._attempted(conn, "p", 14, "2026-07")

    def test_no_redis_means_not_attempted(self):
        # Losing the marker costs re-fetches, never correctness — so the safe
        # answer is "ask again", not "assume done".
        assert not ehb._attempted(None, "g", 14, "2026-07")
        ehb._mark_attempted(None, "g", 14, "2026-07")

    def test_a_broken_redis_is_survivable(self):
        conn = _FakeRedis(blow_up=True)
        assert not ehb._attempted(conn, "g", 14, "2026-07")
        ehb._mark_attempted(conn, "g", 14, "2026-07")


class _Session:
    """Enough SQLAlchemy session to drive the harvest without a database.

    Queries are matched on a distinctive fragment of their SQL rather than in
    call order, so the harvest can be reordered without rewriting the fakes.
    """

    def __init__(self, *, coverage=(), groups=(), stored=(), rosters=None,
                 players=(), bulk_done=False):
        self.coverage = coverage      # [(group_id, count)]
        self.groups = groups          # [(group_id, wom_id)]
        self.stored = stored          # [(player_id, ehb)]
        self.rosters = rosters or {}  # {group_id: [(pid, wom_id, name)]}
        self.players = players        # [(player_id, player_name)]
        self.bulk_done = bulk_done    # a group fetch already wrote this period
        self.written: list = []
        self.commits = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        if "INSERT INTO recap_wom_gains" in sql:
            self.written.extend(params or [])
            return None
        if "SELECT 1 FROM recap_wom_gains" in sql:
            return _Result([(1,)] if self.bulk_done else [])
        if "FROM recap_wom_gains" in sql:
            return _Result(self.stored)
        if "COUNT(*) FROM user_group_association" in sql:
            return _Result(self.coverage)
        if "wom_id FROM groups" in sql:
            return _Result(self.groups)
        if "JOIN players p" in sql:
            return _Result(self.rosters.get(int(params["gid"]), []))
        if "player_name FROM players" in sql:
            return _Result(self.players)
        raise AssertionError(f"unexpected query: {sql}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def _harvest(session, monkeypatch, *, bulk=None, player=None, conn=None, **kwargs):
    """Run one harvest with the two WOM calls replaced by recorders."""
    calls = {"bulk": [], "player": []}

    async def fake_bulk(wom_id, start, end):
        calls["bulk"].append((wom_id, start, end))
        return bulk(wom_id) if callable(bulk) else bulk

    async def fake_player(name, start, end):
        calls["player"].append(name)
        return player(name) if callable(player) else player

    import utils.wiseoldman as wom

    monkeypatch.setattr(wom, "get_group_bulk_gained", fake_bulk, raising=False)
    monkeypatch.setattr(wom, "get_player_gained_ehb", fake_player, raising=False)
    monkeypatch.setattr(ehb, "_redis", lambda: conn)

    kwargs.setdefault("fetch_prev", False)
    stats = asyncio.run(
        ehb.harvest_month_ehb(session, "2026-07", log=lambda m: None, **kwargs)
    )
    return stats, calls


@pytest.fixture(autouse=True)
def _real_recap_helpers(monkeypatch):
    """`services.recap` is a MagicMock under the conftest stub; the harvest needs
    its real period arithmetic to build a WOM window."""
    import types

    stub = types.ModuleType("services.recap")
    stub.is_month_period = lambda p: len(p) == 7 and p[4] == "-"
    stub.previous_month_period = lambda p: "2026-06" if p == "2026-07" else "?"
    stub.month_bounds = lambda p: (f"{p}-01 00:00:00", "2026-08-01 00:00:00")
    monkeypatch.setitem(sys.modules, "services.recap", stub)


class TestHarvest:
    def test_one_group_call_covers_a_whole_roster(self, monkeypatch):
        # The entire point: one request answers for every member, which is why
        # players are reached through their clans before any personal call.
        session = _Session(
            coverage=[(14, 2)], groups=[(14, 500)],
            rosters={14: [(795, 2188996, "Buzzyn"), (796, 42, "Someone")]},
        )
        stats, calls = _harvest(
            session, monkeypatch,
            bulk=[_row(wom_id=2188996, gained=3.0), _row(wom_id=42, gained=1.0)],
            player_ids=[795, 796],
        )
        assert len(calls["bulk"]) == 1
        assert calls["player"] == []
        assert stats["rows_written"] == 2
        assert {w["player_id"]: w["ehb"] for w in session.written} == {795: 3.0, 796: 1.0}
        assert {w["source"] for w in session.written} == {ehb.SOURCE_BULK}

    def test_an_already_harvested_group_is_not_fetched_again(self, monkeypatch):
        # The delivery sweep fires every 15 minutes for three days. Only the
        # first tick may cost anything.
        conn = _FakeRedis()
        ehb._mark_attempted(conn, "g", 14, "2026-07")
        session = _Session(coverage=[], groups=[(14, 500)],
                           rosters={14: [(795, 1, "Buzzyn")]})
        stats, calls = _harvest(session, monkeypatch, bulk=[], conn=conn,
                                group_ids=[14])
        assert calls["bulk"] == []
        assert stats["groups_skipped"] == 1

    def test_a_group_already_in_the_table_is_not_fetched_again(self, monkeypatch):
        # The durable half of the guard. The Redis marker expires; a closed
        # month's gains don't, so rows already in the table settle it.
        session = _Session(coverage=[], groups=[(14, 500)],
                           rosters={14: [(795, 1, "Buzzyn")]}, bulk_done=True)
        stats, calls = _harvest(session, monkeypatch, bulk=[], conn=_FakeRedis(),
                                group_ids=[14])
        assert calls["bulk"] == []
        assert stats["groups_skipped"] == 1
        assert stats["groups_dropped"] == 0

    def test_a_failed_fetch_is_left_for_the_next_run(self, monkeypatch):
        # None is the limiter refusing or WOM faulting — neither is an answer,
        # so it must not be recorded as one.
        conn = _FakeRedis()
        session = _Session(coverage=[], groups=[(14, 500)],
                           rosters={14: [(795, 1, "Buzzyn")]})
        stats, _ = _harvest(session, monkeypatch, bulk=None, conn=conn,
                            group_ids=[14])
        assert stats["fetch_failures"] == 1
        assert not ehb._attempted(conn, "g", 14, "2026-07")

    def test_a_fetch_that_matched_nobody_is_still_an_answer(self, monkeypatch):
        # Otherwise a clan whose members we don't track gets re-fetched on every
        # tick of the window, forever.
        conn = _FakeRedis()
        session = _Session(coverage=[], groups=[(14, 500)],
                           rosters={14: [(795, 1, "Buzzyn")]})
        stats, _ = _harvest(session, monkeypatch, bulk=[], conn=conn,
                            group_ids=[14])
        assert stats["rows_written"] == 0
        assert ehb._attempted(conn, "g", 14, "2026-07")

    def test_a_group_with_no_wom_link_is_never_called(self, monkeypatch):
        session = _Session(coverage=[], groups=[], rosters={})
        stats, calls = _harvest(session, monkeypatch, bulk=[], group_ids=[14])
        assert calls["bulk"] == []
        assert stats["bulk_calls"] == 0

    def test_the_group_cap_reports_what_it_dropped(self, monkeypatch):
        # A silent cap reads as "we covered everyone" when we didn't.
        session = _Session(
            coverage=[], groups=[(14, 500), (15, 501), (16, 502)],
            rosters={g: [(g, g, f"P{g}")] for g in (14, 15, 16)},
        )
        stats, calls = _harvest(session, monkeypatch, bulk=[],
                                group_ids=[14, 15, 16], group_cap=1)
        assert len(calls["bulk"]) == 1
        assert stats["groups_dropped"] == 2

    def test_personal_calls_only_for_who_the_clans_missed(self, monkeypatch):
        session = _Session(
            coverage=[], groups=[], stored=[(795, 3.0)],
            players=[(796, "Uncovered")],
        )
        stats, calls = _harvest(session, monkeypatch, player=2.5,
                                player_ids=[795, 796])
        assert calls["player"] == ["Uncovered"]
        assert session.written == [
            {"player_id": 796, "period": "2026-07", "ehb": 2.5,
             "source": ehb.SOURCE_PLAYER}
        ]

    def test_the_player_cap_reports_what_it_dropped(self, monkeypatch):
        session = _Session(coverage=[], groups=[],
                           players=[(1, "A"), (2, "B"), (3, "C")])
        stats, calls = _harvest(session, monkeypatch, player=1.0,
                                player_ids=[1, 2, 3], player_cap=1)
        assert stats["players_dropped"] == 2

    def test_a_refused_personal_call_writes_nothing(self, monkeypatch):
        session = _Session(coverage=[], groups=[], players=[(796, "Nobody")])
        stats, _ = _harvest(session, monkeypatch, player=None, player_ids=[796])
        assert session.written == []
        assert stats["fetch_failures"] == 1

    def test_the_previous_month_is_fetched_for_the_baseline(self, monkeypatch):
        # Only ever on the first run: after that, last month's harvest already
        # wrote the row this month's comparison reads.
        session = _Session(coverage=[], groups=[(14, 500)],
                           rosters={14: [(795, 1, "Buzzyn")]})
        stats, calls = _harvest(session, monkeypatch, bulk=[], group_ids=[14],
                                fetch_prev=True)
        assert len(calls["bulk"]) == 2

    def test_a_year_period_is_refused(self, monkeypatch):
        # The annual card folds stored monthly rows; there is nothing to fetch.
        session = _Session()
        monkeypatch.setattr(ehb, "_redis", lambda: None)
        stats = asyncio.run(
            ehb.harvest_month_ehb(session, "2026", log=lambda m: None)
        )
        assert stats["bulk_calls"] == 0

    def test_a_broken_query_never_reaches_the_caller(self, monkeypatch):
        # A harvest that fails costs the cards one stat, not the run.
        class Exploding(_Session):
            def execute(self, statement, params=None):
                raise RuntimeError("database is on fire")

        stats, _ = _harvest(Exploding(), monkeypatch, bulk=[], group_ids=[14])
        assert stats["rows_written"] == 0


class TestCandidateGroups:
    def test_explicit_groups_outrank_incidental_coverage(self, monkeypatch):
        # A clan whose own card is being built needs the number whether or not
        # anyone in the DM audience happens to be a member.
        session = _Session(coverage=[(15, 40)], groups=[(14, 500), (15, 501)])
        out = ehb._candidate_groups(session, {14}, {1, 2})
        assert [g for g, _w, _r in out] == [14, 15]

    def test_ordered_by_how_many_players_they_answer_for(self):
        session = _Session(coverage=[(14, 2), (15, 40)],
                           groups=[(14, 500), (15, 501)])
        out = ehb._candidate_groups(session, set(), {1, 2})
        assert [g for g, _w, _r in out] == [15, 14]

    def test_the_global_pseudo_group_is_never_a_candidate(self):
        # Group 2 holds every tracked player; a bulk call for it is absurd.
        session = _Session(coverage=[], groups=[])
        assert ehb._candidate_groups(session, {1, 2}, set()) == []

    def test_nothing_wanted_means_no_queries_at_all(self):
        session = _Session()
        assert ehb._candidate_groups(session, set(), set()) == []
