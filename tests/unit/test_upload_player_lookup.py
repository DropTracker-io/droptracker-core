"""The intake transports must find the player behind a screenshot upload.

Both API transports save the uploaded screenshot BEFORE the processor runs and
so resolve the player themselves. For months they did it with
``Player.player_name == <submitted name>``, which misses whenever the stored
spelling differs from the game's -- WOM group import keeps WOM's
``displayName`` (``ZE_ET``, ``Aff_ma_tits``) while the plugin sends ``ZE ET``;
the submission path stores WOM's folded username (``Beast Owned``) while the
plugin sends ``Beast_Owned``; an un-synced RSN change matches nothing. A miss
was invisible: the temp file was unlinked, the row and the notification went
out with no image, and a group requiring screenshots skipped the post and told
a correctly configured plugin to "enable screenshots" (ticket #430, 2026-09-08).

Measured over the week before the fix, death notifications whose submitted
name differed from the stored one:

    legacy Discord-webhook transport (used_api=0) -> 40 with image,  0 without
    API transport (used_api=1)                   -> 12 with image, 68 without

The legacy transport was immune because it attaches the screenshot after the
processor has resolved the player by account hash. These tests pin the shared
resolver both transports now use, and statically guard the transports against
growing a strict name comparison again.

conftest stubs ``db``, so the real module is loaded by file path with a
sqlite-backed Player model swapped in for the stub.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from utils.format import normalize_player_display_equivalence

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSPORTS = ("workers/webhook_consumer.py", "api/routes/webhook.py")


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
        # MariaDB VIRTUAL generated column in production (web100a); filled here
        # from the same normalizer so a test cannot assert on a normalization
        # production would not produce. See test_formatted_name_lookup.
        player_name_norm = Column(String(64))
        account_hash = Column(String(64))
        wom_id = Column(Integer)
        user = None

    def _player(player_id, player_name, account_hash=None, wom_id=1000):
        return Player(
            player_id=player_id,
            player_name=player_name,
            player_name_norm=normalize_player_display_equivalence(player_name),
            account_hash=account_hash,
            wom_id=wom_id,
        )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    ops = _load_real_module("_ops_upload_under_test", "db/ops.py")
    ops.Player = Player

    rows = [
        # WOM group import kept WOM's displayName; the game spells it "ZE ET".
        _player(1, "ZE_ET", account_hash="105595488451457090", wom_id=869845),
        # Submission path stored WOM's folded username; the game has the underscore.
        _player(2, "Beast Owned", account_hash="-11", wom_id=2710815),
        # RSN changed in game, hourly sync has not caught up yet.
        _player(3, "Ebdonn", account_hash="-33", wom_id=3076067),
        # Plain name, matches exactly -- must keep working.
        _player(4, "Solo", account_hash="-44", wom_id=4444),
        # Split identity: the plugin-authed row still carries a stale RSN and
        # never got a wom_id, while a WOM group import created a stub under the
        # current name that does have one.
        _player(5, "Ghosted Old", account_hash="-55", wom_id=None),
        _player(6, "Ghosted", account_hash="wom_temp_6", wom_id=6666),
    ]
    session.add_all(rows)
    session.commit()

    yield ops, session, Player
    session.close()


def _payload(name, acc_hash=None, key="player_name"):
    data = {key: name}
    if acc_hash is not None:
        data["acc_hash"] = acc_hash
    return data


class TestResolveUploadPlayer:
    @pytest.mark.parametrize(
        "payload, expected_id",
        [
            # The reported case: stored underscore, submitted space, hash present.
            (_payload("ZE ET", "105595488451457090"), 1),
            # Same player when the hash is absent: display equivalence alone.
            (_payload("ZE ET"), 1),
            # The other direction (stored space, submitted underscore).
            (_payload("Beast_Owned", "-11"), 2),
            (_payload("Beast_Owned"), 2),
            # RSN change: only the hash can find this row.
            (_payload("Ebdon GIM", "-33"), 3),
            # Exact spelling keeps resolving.
            (_payload("Solo", "-44"), 4),
            (_payload("Solo"), 4),
            # The consumer reads ``player`` before ``player_name``.
            (_payload("ZE ET", "105595488451457090", key="player"), 1),
        ],
    )
    def test_resolves_across_the_spelling_gap(self, ops_env, payload, expected_id):
        ops, session, _ = ops_env
        player = ops.resolve_upload_player(session, payload)
        assert player is not None, f"{payload!r} did not resolve"
        assert player.player_id == expected_id

    def test_hash_wins_over_a_name_that_belongs_to_someone_else(self, ops_env):
        """The hash is the only key that cannot be spelled two ways."""
        ops, session, _ = ops_env
        player = ops.resolve_upload_player(session, _payload("Solo", "-11"))
        assert player.player_id == 2

    def test_prefers_a_row_with_a_wom_id(self, ops_env):
        """download_image files the upload under wom_id and returns None
        without one, so a hash hit on a wom-less row must not shadow the
        row the name resolves to when that one can actually take the file."""
        ops, session, _ = ops_env
        player = ops.resolve_upload_player(session, _payload("Ghosted", "-55"))
        assert player.player_id == 6

    def test_returns_none_when_nothing_matches(self, ops_env):
        ops, session, _ = ops_env
        assert ops.resolve_upload_player(session, _payload("Nobody Here", "-999")) is None
        assert ops.resolve_upload_player(session, {"acc_hash": "-999"}) is None
        assert ops.resolve_upload_player(session, {}) is None

    def test_unknown_hash_still_falls_back_to_the_name(self, ops_env):
        ops, session, _ = ops_env
        player = ops.resolve_upload_player(session, _payload("ZE ET", "-999"))
        assert player.player_id == 1


def _strict_name_comparisons(tree):
    """Every ``Player.player_name == ...`` (or ``!=``) in the module."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if (
            isinstance(left, ast.Attribute)
            and left.attr == "player_name"
            and isinstance(left.value, ast.Name)
            and left.value.id == "Player"
            and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
        ):
            hits.append(node.lineno)
    return hits


def _calls_named(tree, name):
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


class TestTransportsShareTheResolver:
    @pytest.mark.parametrize("relpath", TRANSPORTS)
    def test_no_strict_name_lookup(self, relpath):
        tree = ast.parse((REPO_ROOT / relpath).read_text())
        hits = _strict_name_comparisons(tree)
        assert not hits, (
            f"{relpath} compares Player.player_name with == at line(s) {hits}; "
            "a submitted RSN and the stored name are different strings for any "
            "player whose spelling diverged -- go through resolve_upload_player"
        )

    @pytest.mark.parametrize("relpath", TRANSPORTS)
    def test_resolves_the_upload_player_through_the_shared_helper(self, relpath):
        tree = ast.parse((REPO_ROOT / relpath).read_text())
        assert _calls_named(tree, "resolve_upload_player"), (
            f"{relpath} no longer calls resolve_upload_player before download_image"
        )
        assert _calls_named(tree, "download_image"), (
            f"{relpath} no longer saves uploads at all -- the guard above is moot"
        )
