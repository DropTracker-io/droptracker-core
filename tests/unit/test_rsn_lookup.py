"""Typed RSNs must find a player under any spelling the game considers the same.

OSRS treats '-', '_' and ' ' as one character in a display name: the hiscores
return the same account for "1-19", "1 19" and "1_19", and two accounts can
never differ only by them. WOM folds all three to a space, and that folded
spelling is usually what ``players.player_name`` stores. Every lookup that
matched a typed name exactly (or with ``ilike``, which also reads '_' as a
wildcard) missed hyphenated players typed the way the game shows them: "1-19"
could not claim its own "1 19" row and was told it had never used the plugin
(ticket #434, 2026-09-13), and the same gap sat under site search, the points
commands, event bulk add, /player_search, /player and the Data API.

conftest stubs ``db``; the helpers take the model (or rows) as arguments, so a
sqlite-backed stand-in is passed straight in.
"""

import types

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from utils.rsn import (
    find_player_by_rsn,
    normalize_player_display_equivalence,
    pick_player_by_rsn,
    player_name_search_expr,
    rsn_contains,
)

ROWS = [
    # The ticket: submission path stored WOM's folded username.
    (5766416, "1 19", "3212333226023630924"),
    (5755902, "tzuk kal lag", "-1"),
    # WOM group import kept an underscore the game does not show.
    (10, "Itz_Baal", "-2"),
    (11, "Solo", "-3"),
    # Split identity: the original account and a later wom_temp stub with the
    # same name. The original (lowest id) must win. Inserted stub first so
    # insertion order cannot pass the test by accident.
    (21, "brondt", "wom_temp_169385"),
    (20, "Brondt", "-4"),
    (23, "ze_et", "wom_temp_869845"),
    (22, "ZE ET", "-7"),
    # Two rows that differ only by a separator: the exact spelling wins.
    (30, "a b", "-5"),
    (31, "a_b", "-6"),
]


@pytest.fixture
def claim_env():
    Base = declarative_base()

    class Player(Base):
        __tablename__ = "players"
        player_id = Column(Integer, primary_key=True)
        # NOCASE mirrors production's utf8mb4_general_ci, which makes the exact
        # step's plain ``=`` case-insensitive.
        player_name = Column(String(64, collation="NOCASE"))
        # MariaDB VIRTUAL generated column in production (web100a); filled here
        # from the same normalizer so a test cannot assert on a normalization
        # production would not produce.
        player_name_norm = Column(String(64))
        account_hash = Column(String(64))

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        Player(player_id=pid, player_name=name, account_hash=acc,
               player_name_norm=normalize_player_display_equivalence(name))
        for pid, name, acc in ROWS
    ])
    session.commit()
    yield session, Player
    session.close()


EQUIVALENT_SPELLINGS = [
    # The reported case, under every spelling the game treats as equal.
    ("1-19", 5766416),
    ("1 19", 5766416),
    ("1_19", 5766416),
    ("  1-19  ", 5766416),
    ("1\u00a019", 5766416),  # Discord can hand over a non-breaking space
    ("Tzuk-Kal-Lag", 5755902),
    ("tzuk_kal-lag", 5755902),
    # The reverse direction: stored underscore, typed space or hyphen.
    ("Itz Baal", 10),
    ("itz-baal", 10),
    # Plain names keep resolving, case-insensitively.
    ("Solo", 11),
    ("SOLO", 11),
]

NON_EQUIVALENT = ["1-9", "119", "S_lo", "S%lo", "", "   ", None]


class TestFindPlayerByRsn:
    @pytest.mark.parametrize("typed, expected_id", EQUIVALENT_SPELLINGS)
    def test_equivalent_spellings_resolve(self, claim_env, typed, expected_id):
        session, Player = claim_env
        player = find_player_by_rsn(session, Player, typed)
        assert player is not None, f"{typed!r} did not resolve"
        assert player.player_id == expected_id

    def test_exact_spelling_beats_a_folded_twin(self, claim_env):
        session, Player = claim_env
        assert find_player_by_rsn(session, Player, "a_b").player_id == 31
        assert find_player_by_rsn(session, Player, "A B").player_id == 30

    def test_case_insensitive_tie_goes_to_the_original_account(self, claim_env):
        session, Player = claim_env
        assert find_player_by_rsn(session, Player, "BRONDT").player_id == 20

    def test_folded_tie_goes_to_the_original_account(self, claim_env):
        # "ze-et" is neither stored spelling, so only the folded step can match,
        # and both rows fold to "ze et".
        session, Player = claim_env
        assert find_player_by_rsn(session, Player, "ze-et").player_id == 22

    @pytest.mark.parametrize("typed", NON_EQUIVALENT)
    def test_non_equivalent_names_do_not_match(self, claim_env, typed):
        # '_' and '%' are LIKE wildcards: the old ilike fallback matched "S_lo"
        # to "Solo". A separator is not a wildcard.
        session, Player = claim_env
        assert find_player_by_rsn(session, Player, typed) is None


def _rows_in_hand():
    return [types.SimpleNamespace(player_id=pid, player_name=name) for pid, name, _ in ROWS]


class TestPickPlayerByRsn:
    """Same rules over rows already loaded: a user's accounts, a group roster."""

    @pytest.mark.parametrize("typed, expected_id", EQUIVALENT_SPELLINGS)
    def test_equivalent_spellings_resolve(self, typed, expected_id):
        player = pick_player_by_rsn(_rows_in_hand(), typed)
        assert player is not None, f"{typed!r} did not resolve"
        assert player.player_id == expected_id

    def test_exact_spelling_beats_a_folded_twin(self):
        assert pick_player_by_rsn(_rows_in_hand(), "a_b").player_id == 31
        assert pick_player_by_rsn(_rows_in_hand(), "A B").player_id == 30

    def test_ties_go_to_the_lowest_id_whatever_the_list_order(self):
        rows = list(reversed(_rows_in_hand()))
        assert pick_player_by_rsn(rows, "BRONDT").player_id == 20
        assert pick_player_by_rsn(rows, "ze-et").player_id == 22

    @pytest.mark.parametrize("typed", NON_EQUIVALENT)
    def test_non_equivalent_names_do_not_match(self, typed):
        assert pick_player_by_rsn(_rows_in_hand(), typed) is None

    def test_only_searches_the_rows_it_is_given(self):
        # /hideme and /submit resolve among the caller's own accounts: another
        # user's "Solo" must not be reachable by typing its name.
        mine = [types.SimpleNamespace(player_id=5766416, player_name="1 19")]
        assert pick_player_by_rsn(mine, "Solo") is None
        assert pick_player_by_rsn(mine, "1-19").player_id == 5766416

    def test_empty_and_none_rows(self):
        assert pick_player_by_rsn([], "1-19") is None
        assert pick_player_by_rsn([None], "1-19") is None


class TestRsnContains:
    @pytest.mark.parametrize("name, text", [
        ("tzuk kal lag", "Tzuk-Kal"),
        ("tzuk kal lag", "kal_lag"),
        ("1 19", "1-1"),
        ("Itz_Baal", "itz baal"),
        ("Solo", "OL"),
    ])
    def test_matches_across_separators_and_case(self, name, text):
        assert rsn_contains(name, text)

    @pytest.mark.parametrize("name, text", [
        ("Solo", "S_lo"),  # a separator is not a wildcard
        ("1 19", "119"),
        ("tzuk kal lag", "tzukkal"),
    ])
    def test_does_not_match_different_names(self, name, text):
        assert not rsn_contains(name, text)

    @pytest.mark.parametrize("text", ["", "   ", "-", "_ -", None])
    def test_text_that_folds_to_nothing_filters_nothing(self, text):
        assert rsn_contains("Solo", text)


class TestPlayerNameSearchExpr:
    @pytest.mark.parametrize("text, expected_ids", [
        ("Tzuk-Kal", {5755902}),
        ("1-1", {5766416}),
        ("itz baal", {10}),
        ("BRONDT", {20, 21}),
        ("a-b", {30, 31}),
        ("S_lo", set()),   # '_' is escaped, not a wildcard
        ("S%lo", set()),   # so is '%'
    ])
    def test_substring_search_folds_both_sides(self, claim_env, text, expected_ids):
        session, Player = claim_env
        needle = normalize_player_display_equivalence(text)
        rows = (
            session.query(Player.player_id)
            .filter(player_name_search_expr(Player.player_name).contains(needle, autoescape=True))
            .all()
        )
        assert {pid for (pid,) in rows} == expected_ids


class TestDataApiPlayerRef:
    """/v2/players/<ref> resolves ids, then names under the same rules."""

    @pytest.mark.parametrize("ref, expected", [
        ("1-19", 5766416),
        ("Tzuk-Kal-Lag", 5755902),
        ("Itz Baal", 10),
        ("a_b", 31),
        ("ze-et", 22),
        ("5766416", 5766416),
        ("Nobody-Here", None),
        ("", None),
    ])
    def test_resolves_names_across_separators(self, claim_env, ref, expected):
        import data_api.scope as scope

        session, _Player = claim_env
        assert scope.resolve_player_ref(session, ref) == expected


class TestImportsWithoutTheDatabaseLayer:
    """``utils.format`` imports ``db``, whose package init imports ``db.ops``,
    which imports ``utils.embeds``, which imports ``utils.format``: the first
    ``utils.format`` import in a process that has not already loaded ``db``
    dies on the cycle. The Data API is such a process -- a lazy ``utils.format``
    import in ``resolve_player_ref`` turned every ``/v2/players/<ref>`` into a
    500 (2026-09-13). conftest stubs ``db``, so only a fresh interpreter sees it.
    """

    @staticmethod
    def _run(code: str):
        import subprocess
        import sys
        from pathlib import Path

        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=120,
        )

    def test_rsn_helpers_import_nothing_heavy(self):
        result = self._run(
            "import sys\n"
            "import utils.rsn\n"
            "heavy = [m for m in ('db', 'utils.format', 'interactions', 'PIL') if m in sys.modules]\n"
            "assert not heavy, heavy\n"
        )
        assert result.returncode == 0, result.stderr

    def test_data_api_player_ref_resolves_without_db(self):
        result = self._run(
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('scope_under_test', 'data_api/scope.py')\n"
            "scope = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(scope)\n"
            "class Result:\n"
            "    def __init__(self, row): self.row = row\n"
            "    def first(self): return self.row\n"
            "class Session:\n"
            "    def execute(self, stmt):\n"
            "        return Result((5766416,) if 'player_name_norm' in str(stmt) else None)\n"
            "assert scope.resolve_player_ref(Session(), '1-19') == 5766416\n"
            "assert 'db' not in sys.modules and 'utils.format' not in sys.modules\n"
        )
        assert result.returncode == 0, result.stderr
