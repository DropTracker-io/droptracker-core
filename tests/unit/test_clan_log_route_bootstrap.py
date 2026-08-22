"""web_api/routes/clan_log.py `_board`: which groups get a board built for them.

The gate here decides whether a clan sees its Clan Log or a 404, and it used to
be wrong in a way nobody could hit locally. Boards are only ever written by the
sweep in bots/main.py, whose candidate list came from `clan_log_firsts` — the
very table the sweep populates. Nothing put a group into that table, so every
clan created after the one manual backfill had no ledger, and this function's
`period not in ledger_periods(...)` check turned that into a 404 on
/groups/<id>/log for 209 groups with real members and real drops.

The rule now: an all-time board is built for any group with a roster, empty or
not, and NOT stored (the sweep owns the stored board — pinning a 0% snapshot
here is exactly the "real-looking 0%" the original gate was guarding against).
Narrower periods still 404 without a ledger, because `is_valid_period` accepts
any well-formed YYYY-MM and building those on demand would let a crafted URL
mint unbounded snapshot rows.
"""

import contextlib
import sys
import types

import pytest

import web_api.routes.clan_log as route


class _Session:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.fixture
def board(monkeypatch):
    """Drive `_board` against a stubbed services.clan_log, reporting side effects."""

    def _run(*, stored=None, ledger=(), roster=(), period="all", group_id=7):
        calls = {"built": [], "saved": [], "session": _Session()}

        stub = types.ModuleType("services.clan_log")
        stub.PERIOD_ALL = "all"
        stub.is_valid_period = lambda p: p == "all" or (
            len(p) in (4, 7) and p.replace("-", "").isdigit()
        )
        stub.load_board = lambda s, gid, p: stored
        stub.ledger_periods = lambda s, gid: list(ledger)
        stub.visible_group_player_ids = lambda s, gid: list(roster)

        def _build(s, gid, p):
            calls["built"].append((gid, p))
            return {"summary": {"obtained": 0, "total": 326}, "period": p}

        def _save(s, gid, p, payload):
            calls["saved"].append((gid, p))

        stub.build_payload = _build
        stub.save_board = _save

        # The conftest stubs `services` as a MagicMock, so `from
        # services.clan_log import ...` inside the handler resolves to whatever
        # sys.modules holds — inject a real module and put it back after.
        saved = sys.modules.get("services.clan_log")
        monkeypatch.setattr(route, "db_session",
                            contextlib.contextmanager(lambda: iter([calls["session"]])))
        sys.modules["services.clan_log"] = stub
        try:
            calls["result"] = route._board(group_id, period)
        finally:
            if saved is None:
                del sys.modules["services.clan_log"]
            else:
                sys.modules["services.clan_log"] = saved
        return calls

    return _run


def test_a_stored_board_is_returned_untouched(board):
    calls = board(stored={"summary": {"obtained": 12, "total": 326}}, ledger=["all"])
    assert calls["result"]["summary"]["obtained"] == 12
    assert calls["built"] == [] and calls["saved"] == []


def test_a_period_the_ledger_answers_for_is_built_and_stored(board):
    calls = board(ledger=["all", "2026", "2026-08"], period="2026-08")
    assert calls["built"] == [(7, "2026-08")]
    assert calls["saved"] == [(7, "2026-08")]
    assert calls["session"].commits == 1


def test_a_group_with_members_but_no_ledger_still_gets_an_all_time_board(board):
    """The regression: 209 groups were 404ing here purely for never being swept."""
    calls = board(ledger=[], roster=[101, 102], period="all")
    assert calls["result"]["summary"] == {"obtained": 0, "total": 326}
    assert calls["built"] == [(7, "all")]


def test_that_bootstrap_board_is_never_stored(board):
    """Storing it would pin a real-looking 0% in front of the sweep's real one."""
    calls = board(ledger=[], roster=[101], period="all")
    assert calls["saved"] == []
    assert calls["session"].commits == 0


def test_a_group_with_no_roster_is_still_a_404(board):
    """No members means no clan — an unknown id must not mint a board."""
    calls = board(ledger=[], roster=[], period="all")
    assert calls["result"] is None
    assert calls["built"] == []


@pytest.mark.parametrize("period", ["2019-03", "2031"])
def test_a_narrow_period_with_no_ledger_is_still_a_404(board, period):
    """Otherwise any well-formed YYYY-MM in the URL mints a snapshot row."""
    calls = board(ledger=[], roster=[101, 102], period=period)
    assert calls["result"] is None
    assert calls["built"] == []


def test_an_invalid_period_never_reaches_the_database(board):
    calls = board(ledger=["all"], roster=[101], period="not-a-period")
    assert calls["result"] is None
    assert calls["built"] == []
