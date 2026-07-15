"""Regression tests for the scoped-session idle-transaction leak fix.

Background (memory: webapi-session-leak-fix): several helpers fall back to the
module-level *scoped* session (``db.session`` / ``models.session``) when the
caller passes no session. A bare ``.query()`` autobegins a read transaction that
those code paths never commit/roll back, so the scoped session keeps its
connection checked out with an idle transaction on the calling thread. The
2026-07-15 incident fixed this in ``services/points.py`` (web_api); these tests
lock in the equivalent fix for the two helpers that run in the bot /
player-updates / intake processes:

* ``utils.wiseoldman.fetch_group_members`` (hot, live)
* ``utils.redis.calculate_rank_amongst_groups`` (currently dead, fixed defensively)

The invariant, in both cases:
  - called WITHOUT a caller session  -> the scoped session's connection is
    returned to the pool afterwards (``pool.checkedout() == 0``).
  - called WITH a caller session      -> the scoped session is left untouched;
    releasing it is the caller's responsibility.

The whole unit suite stubs ``db``, ``utils.redis`` and ``utils.wiseoldman`` with
MagicMocks (see tests/conftest.py), so these tests load the *real* modules by
file path with a real sqlite-backed scoped session injected in place of the
stubs, then restore sys.modules on teardown.
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, Table, create_engine
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_real_module(throwaway_name: str, relpath: str):
    """Load a real source module under a throwaway name, bypassing the conftest stub."""
    spec = importlib.util.spec_from_file_location(throwaway_name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[throwaway_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def leak_env():
    """A real sqlite-backed scoped session wired in as ``db``/``db.models`` while
    the real ``utils.wiseoldman`` and ``utils.redis`` modules are loaded."""
    Base = declarative_base()
    user_group_association = Table(
        "user_group_association",
        Base.metadata,
        Column("player_id", Integer, ForeignKey("players.player_id")),
        Column("group_id", Integer, ForeignKey("groups.group_id")),
    )

    class Group(Base):
        __tablename__ = "groups"
        group_id = Column(Integer, primary_key=True)
        wom_id = Column(Integer)
        group_name = Column(String(50))

    class Player(Base):
        __tablename__ = "players"
        player_id = Column(Integer, primary_key=True)
        wom_id = Column(Integer)

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    scoped = scoped_session(Session)

    seed = Session()
    seed.add_all([Player(player_id=1, wom_id=111), Player(player_id=2, wom_id=222)])
    seed.commit()
    seed.close()

    # Fake dependency modules the real modules import.
    models_ns = types.ModuleType("db.models")
    models_ns.session = scoped
    models_ns.Group = Group
    models_ns.Player = Player
    models_ns.user_group_association = user_group_association

    db_mod = types.ModuleType("db")
    db_mod.Player = Player
    db_mod.Group = Group
    db_mod.session = scoped
    db_mod.models = models_ns
    db_mod.NpcList = type("NpcList", (), {})  # utils.format does `from db import NpcList`

    utils_redis_stub = types.ModuleType("utils.redis")

    class _NoopRedis:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    utils_redis_stub.redis_client = _NoopRedis()

    services_ru_stub = types.ModuleType("services.redis_updates")
    services_ru_stub.get_player_list_loot_sum = lambda ids: 0

    saved = {}

    def swap(name, module):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module

    swap("db", db_mod)
    swap("db.models", models_ns)
    swap("utils.redis", utils_redis_stub)
    swap("services.redis_updates", services_ru_stub)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    wiseoldman = _load_real_module("_leaktest_wiseoldman", "utils/wiseoldman.py")
    redis_mod = _load_real_module("_leaktest_redis", "utils/redis.py")

    env = types.SimpleNamespace(
        wiseoldman=wiseoldman,
        redis=redis_mod,
        scoped=scoped,
        engine=engine,
        Player=Player,
        Group=Group,
    )
    try:
        yield env
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_leaktest_wiseoldman", None)
        sys.modules.pop("_leaktest_redis", None)
        scoped.remove()
        engine.dispose()
        os.remove(db_path)


# ── utils.wiseoldman.fetch_group_members ─────────────────────────────────────

def test_fetch_group_members_is_wrapped_for_cleanup(leak_env):
    # The cleanup decorator must actually be applied.
    assert hasattr(leak_env.wiseoldman.fetch_group_members, "__wrapped__")


def test_fetch_group_members_releases_scoped_session_when_owned(leak_env):
    scoped, engine = leak_env.scoped, leak_env.engine
    scoped.remove()
    assert engine.pool.checkedout() == 0

    # wom_group_id == 1 + force_refresh hits a single scoped-session read then returns.
    result = asyncio.run(leak_env.wiseoldman.fetch_group_members(1, force_refresh=True))

    assert result == [111, 222]
    assert engine.pool.checkedout() == 0, (
        "owned scoped session left an idle transaction / checked-out connection"
    )


def test_fetch_group_members_leaves_caller_supplied_session_untouched(leak_env):
    scoped, engine = leak_env.scoped, leak_env.engine
    scoped.remove()
    assert engine.pool.checkedout() == 0

    # Caller owns the session (passes it in): the helper must NOT clean it up.
    result = asyncio.run(
        leak_env.wiseoldman.fetch_group_members(1, session_to_use=scoped, force_refresh=True)
    )

    assert result == [111, 222]
    assert engine.pool.checkedout() == 1, (
        "helper cleaned up a caller-supplied session; that is the caller's job"
    )
    scoped.remove()  # caller cleans up
    assert engine.pool.checkedout() == 0


# ── utils.redis.calculate_rank_amongst_groups (dead, fixed defensively) ───────

def test_calculate_rank_releases_scoped_session_when_owned(leak_env):
    scoped, engine = leak_env.scoped, leak_env.engine
    scoped.remove()
    assert engine.pool.checkedout() == 0

    # No groups seeded: the helper still issues `query(Group).all()` (opens a read
    # transaction) before returning, so an unfixed version would leave it idle.
    rank, total = leak_env.redis.calculate_rank_amongst_groups(1, [])

    assert (rank, total) == (None, 0)
    assert engine.pool.checkedout() == 0, (
        "owned scoped session left an idle transaction / checked-out connection"
    )


def test_calculate_rank_leaves_caller_supplied_session_untouched(leak_env):
    scoped, engine = leak_env.scoped, leak_env.engine
    scoped.remove()
    assert engine.pool.checkedout() == 0

    rank, total = leak_env.redis.calculate_rank_amongst_groups(1, [], session_to_use=scoped)

    assert (rank, total) == (None, 0)
    assert engine.pool.checkedout() == 1, (
        "helper cleaned up a caller-supplied session; that is the caller's job"
    )
    scoped.remove()
    assert engine.pool.checkedout() == 0
