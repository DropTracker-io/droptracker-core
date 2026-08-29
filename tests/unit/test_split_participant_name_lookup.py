"""Participant resolution on the split paths (drop.py + point_awards.py).

Same spelling gap as tests/unit/test_formatted_name_lookup.py, different
victim. The RuneLite plugin submits the RSN exactly as the game spells it —
``X-tra``, ``Beast_Owned``, verbatim from ``getLocalPlayerName()``, with no
normalization anywhere in the client — while WOM, our identity source, folds
both ``-`` and ``_`` to spaces, so the row we store is ``x tra``.
``utf8mb4_general_ci`` does not treat those as equal.

Both split resolvers used to fold underscores only:

    alt = name.replace(" ", "_") if " " in name else name.replace("_", " ")

so every hyphenated RSN resolved to nothing and was skipped — no split GP
credit and no split point award. 53 stored names carry a hyphen; the live
casualties included X-tra, Tzuk-Kal-Lag, NoX-EvilAce, Quack-Trades,
Toktz-ik-unt, Blip-A, Jads-nut, shawnos-iron and XX-ARABE-XX.

Fixing the lookup makes strictly more names resolve, which is what these tests
mostly guard: names that used to fall through the floor now land somewhere, so
the receiver must be excluded by **id** (name-based filtering upstream is the
thing that can't see through the spelling gap) and one account named twice
under two spellings must still be credited once. Getting either wrong turns a
missing-credit bug into a double-credit bug for the same players.

conftest stubs ``db``, so the real ``db/ops.py`` resolver is loaded by file
path with a sqlite-backed Player model swapped in, then injected where the
production modules look for it.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from data.submissions import drop as drop_module
from data.submissions import point_awards
from data.submissions.manual_discord import parse_split_players
from data.submissions.point_awards import _normalize_player_names
from utils.format import normalize_player_display_equivalence

REPO_ROOT = Path(__file__).resolve().parents[2]

# The receiver of the drop under test, and the id nobody else may collide with.
RECEIVER_ID = 100


@pytest.fixture
def resolver_env(monkeypatch):
    """A sqlite `players` table wired into the real db/ops.py resolver.

    Yields the session; both call sites are patched to use this resolver, so a
    test can call the production helpers directly.
    """
    Base = declarative_base()

    class Player(Base):
        __tablename__ = "players"
        player_id = Column(Integer, primary_key=True)
        player_name = Column(String(64))
        # In production this is a MariaDB VIRTUAL generated column (web100a).
        # sqlite cannot express its REGEXP_REPLACE whitespace collapse, so here
        # it is a plain column filled by ``_player()`` from the very normalizer
        # the generated column mirrors -- never written by hand, so a test can
        # not assert on a normalization production would not produce. That the
        # DDL and the normalizer agree is a separate claim, asserted against a
        # real MariaDB in tests/integration/player_name_norm_it.py.
        player_name_norm = Column(String(64))
        user = None

    def _player(player_id, player_name):
        return Player(
            player_id=player_id,
            player_name=player_name,
            player_name_norm=normalize_player_display_equivalence(player_name),
        )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    spec = importlib.util.spec_from_file_location("_ops_for_split_tests", REPO_ROOT / "db/ops.py")
    ops = importlib.util.module_from_spec(spec)
    sys.modules["_ops_for_split_tests"] = ops
    spec.loader.exec_module(ops)
    ops.Player = Player

    session.add_all([
        # WOM-canonical spelling: what the submission path stores, and what
        # every one of the live casualties actually looks like in `players`.
        _player(RECEIVER_ID, "wi beer guy"),
        _player(1, "x tra"),
        _player(2, "tzuk kal lag"),
        _player(3, "NoX EvilAce"),
        # WOM group import keeps display_name, so separators survive there —
        # the reverse direction still has to fold.
        _player(4, "Itz_Baal"),
        _player(5, "Blip-A"),
        _player(6, "Solo"),
        # player_id 0 is a real account (the project owner's), so the receiver
        # check has to compare ids, not their truthiness.
        _player(0, "zero acc"),
    ])
    session.commit()

    # drop.py imports the resolver inside the function, off the (stubbed)
    # db.ops module; point_awards.py binds it at import time.
    monkeypatch.setattr(sys.modules["db.ops"], "resolve_player_for_display",
                        ops.resolve_player_for_display, raising=False)
    monkeypatch.setattr(point_awards, "resolve_player_for_display",
                        ops.resolve_player_for_display)

    yield session
    session.close()


# ── the reported bug: hyphenated participants resolve ────────────────────────

@pytest.mark.parametrize("submitted, expected_id", [
    ("X-tra", 1),           # plugin spelling vs stored space  <- the bug
    ("X_tra", 1),           # the case the old code did handle
    ("x tra", 1),           # already-folded
    ("Tzuk-Kal-Lag", 2),    # every separator folds, not just the first
    ("NoX-EvilAce", 3),
    ("Itz Baal", 4),        # reverse direction: stored separator, submitted space
    ("Blip A", 5),
    ("Blip_A", 5),          # stored hyphen, submitted underscore
    ("Solo", 6),            # no separator, still the fast path
])
def test_point_award_lookup_resolves_across_the_spelling_gap(resolver_env, submitted, expected_id):
    assert point_awards._get_player_id_by_name(submitted, resolver_env) == expected_id


def test_point_award_lookup_returns_none_for_an_unknown_name(resolver_env):
    """Callers branch on None to skip a participant — it must not raise."""
    for unknown in ("Nobody At All", "No-body", "", None):
        assert point_awards._get_player_id_by_name(unknown, resolver_env) is None


@pytest.mark.parametrize("submitted, expected_id", [
    ("X-tra", 1),
    ("Tzuk-Kal-Lag", 2),
    ("NoX-EvilAce", 3),
    ("Itz Baal", 4),
])
def test_split_gp_participants_resolve_across_the_spelling_gap(resolver_env, submitted, expected_id):
    resolved = drop_module._resolve_split_participants(resolver_env, [submitted], RECEIVER_ID)
    assert [p.player_id for p in resolved] == [expected_id]


def test_unresolvable_names_are_skipped_not_fatal(resolver_env):
    """An untracked player in the nearby list is normal, not an error."""
    resolved = drop_module._resolve_split_participants(
        resolver_env, ["X-tra", "Some Rando", "Tzuk-Kal-Lag"], RECEIVER_ID
    )
    assert [p.player_id for p in resolved] == [1, 2]


@pytest.mark.parametrize("players_included", [None, [], ["", "   "]])
def test_no_participants_resolves_to_nothing(resolver_env, players_included):
    assert drop_module._resolve_split_participants(resolver_env, players_included, RECEIVER_ID) == []


# ── the risk the fix introduces: names that now resolve must not double-pay ──

@pytest.mark.parametrize("receiver_spelling", ["WI Beer Guy", "WI_Beer_Guy", "WI-Beer-Guy", "wi beer guy"])
def test_receiver_is_never_credited_as_their_own_participant(resolver_env, receiver_spelling):
    """The receiver anchors the divisor and takes the receiver adjustment; a
    second share would pay them twice out of a pot that only holds one."""
    resolved = drop_module._resolve_split_participants(
        resolver_env, [receiver_spelling, "X-tra"], RECEIVER_ID
    )
    assert [p.player_id for p in resolved] == [1]


def test_one_account_named_twice_is_credited_once(resolver_env):
    """Both spellings now resolve to the same row — before the fix only one
    did, so the duplicate hid behind a failed lookup."""
    resolved = drop_module._resolve_split_participants(
        resolver_env, ["X-tra", "x tra", "X_tra"], RECEIVER_ID
    )
    assert [p.player_id for p in resolved] == [1]


def test_receiver_id_of_zero_still_excludes_the_receiver(resolver_env):
    """player_id 0 is a real account, so `if receiver_id` would fail open and
    credit them twice."""
    resolved = drop_module._resolve_split_participants(resolver_env, ["zero-acc", "X-tra"], 0)
    assert [p.player_id for p in resolved] == [1]


# ── _normalize_player_names: the same exclusion, one layer up ────────────────

@pytest.mark.parametrize("submitted_receiver", ["X-tra", "X_tra", "x tra", "X  tra"])
def test_receiver_is_dropped_from_the_point_participant_list(submitted_receiver):
    """receiver_player_name comes from the DB in WOM's folded spelling while
    participants arrive in the plugin's — a plain lowercase match misses."""
    assert _normalize_player_names([submitted_receiver, "Tzuk-Kal-Lag"], "x tra") == ["Tzuk-Kal-Lag"]


def test_participant_list_dedupes_across_spellings():
    assert _normalize_player_names(["X-tra", "x tra", "X_tra"], "someone else") == ["X-tra"]


def test_normalization_keeps_the_submitted_spelling():
    """Only the comparison key is folded; the name is passed on as submitted so
    logs and award reasons show what the player actually typed."""
    assert _normalize_player_names(["Tzuk-Kal-Lag"], "x tra") == ["Tzuk-Kal-Lag"]


# ── the manual surfaces, which build the divisor client-side ─────────────────

@pytest.mark.parametrize("listed_self", ["X-tra", "X_tra", "x tra"])
def test_manual_split_drops_a_hyphenated_receiver_from_their_own_list(listed_self):
    """`/submit drop` derives split_size from this list, so a receiver left in
    it inflates the divisor and shrinks everyone's share."""
    assert parse_split_players(f"{listed_self}, puzzled life", "X-tra") == ["puzzled life"]
    assert parse_split_players(f"{listed_self}, puzzled life", "x tra") == ["puzzled life"]
