"""Player resolution behind Discord notification names (db/ops.py).

The RuneLite plugin submits the RSN exactly as the game spells it
(``Beast_Owned``); we store WOM's canonical form, which folds ``_`` and ``-``
to spaces (``Beast Owned``). ``utf8mb4_general_ci`` does not treat those as
equal, so the old strict ``player_name ==`` lookup missed for every
underscore/hyphen RSN and then dereferenced None. ``notification_service``
classifies the resulting AttributeError as non-transient, so the queue row was
marked `failed` and the message dropped for good — 596 lost notifications
across 40 players in the 30 days the queue retains.

Two rules carry the risk:

* resolution must survive the ``_``/``-`` vs space spelling gap in both
  directions (WOM-canonical rows and WOM-group-import rows disagree);
* an unresolvable name must degrade to a plain string, never raise, or one
  unknown player silently costs a group every notification it produces.

conftest stubs ``db``, so the real module is loaded by file path with a
sqlite-backed Player model swapped in for the stub.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_real_module(throwaway_name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(throwaway_name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[throwaway_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ops_env():
    Base = declarative_base()

    class Player(Base):
        __tablename__ = "players"
        player_id = Column(Integer, primary_key=True)
        player_name = Column(String(64))
        # get_formatted_name only reads .user to decide on an @-mention;
        # None means "no linked Discord account, never ping".
        user = None

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    ops = _load_real_module("_ops_under_test", "db/ops.py")
    ops.Player = Player
    # utils.site_urls is real, but pin the label format the assertions read.
    ops.player_link = lambda name, pid: f"[{name}](/players/{pid})"

    rows = [
        # WOM-canonical spelling: what the submission path stores.
        Player(player_id=1, player_name="Beast Owned"),
        Player(player_id=2, player_name="tzuk kal lag"),
        # WOM group import keeps display_name, so underscores survive there.
        Player(player_id=3, player_name="Itz_Baal"),
        Player(player_id=4, player_name="Solo"),
    ]
    session.add_all(rows)
    session.commit()

    yield ops, session, Player
    session.close()


@pytest.mark.parametrize(
    "submitted, expected_id",
    [
        ("Beast Owned", 1),   # exact
        ("Beast_Owned", 1),   # underscore RSN vs stored space  <- the reported bug
        ("Beast-Owned", 1),   # hyphen RSN vs stored space
        ("beast_owned", 1),   # collation is case-insensitive; we must be too
        ("Tzuk-Kal-Lag", 2),  # every separator folds, not just the first
        ("Itz Baal", 3),      # reverse direction: stored underscore, submitted space
        ("Itz_Baal", 3),
        ("Solo", 4),
    ],
)
def test_resolves_across_the_spelling_gap(ops_env, submitted, expected_id):
    ops, session, _ = ops_env
    player = ops.resolve_player_for_display(session, submitted)
    assert player is not None, f"{submitted!r} did not resolve"
    assert player.player_id == expected_id


def test_player_id_wins_over_a_stale_name(ops_env):
    """Payloads carry both; the id is the only one that can't be spelled two ways."""
    ops, session, _ = ops_env
    player = ops.resolve_player_for_display(session, "some name they used to have", player_id=2)
    assert player.player_id == 2


def test_falls_back_to_the_name_when_the_id_is_junk(ops_env):
    ops, session, _ = ops_env
    for junk in (None, "", "not-an-int", 999999):
        player = ops.resolve_player_for_display(session, "Beast_Owned", player_id=junk)
        assert player is not None and player.player_id == 1, junk


def test_unknown_player_resolves_to_none(ops_env):
    ops, session, _ = ops_env
    assert ops.resolve_player_for_display(session, "Nobody At All") is None
    assert ops.resolve_player_for_display(session, "") is None
    assert ops.resolve_player_for_display(session, None) is None


def test_formatted_name_links_the_stored_spelling(ops_env):
    """The link label is the canonical name, not the plugin's spelling."""
    ops, session, _ = ops_env
    assert ops.get_formatted_name("Beast_Owned", 293, session) == "[Beast Owned](/players/1)"


def test_formatted_name_never_raises_for_an_unknown_player(ops_env):
    """A None row used to raise AttributeError here, which killed the message."""
    ops, session, _ = ops_env
    assert ops.get_formatted_name("Nobody At All", 293, session) == "Nobody At All"
    assert ops.get_formatted_name(None, 293, session) == "Unknown"
