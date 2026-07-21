"""EHB refresh riding on the hourly WOM group-member sync (web60a).

``fetch_group_members`` already pays for one ``groups.get_details`` call per
linked clan per hour, and every membership in that response carries a full WOM
player object (``wom.GroupMembership.player``) including ``ehb``. These tests
lock in that the sync captures it — that's what keeps players.ehb fresh, since
the submission hot path deliberately skips WOM for established players.

Like test_session_leak_scoped_cleanup.py, the conftest stubs ``utils.wiseoldman``
with a MagicMock, so the real module is loaded by file path with a sqlite-backed
session and a faked WOM client in place of the stubs.
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest
from sqlalchemy import Column, Float, ForeignKey, Integer, String, Table, create_engine
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]

WOM_GROUP_ID = 77


def _load_real_module(throwaway_name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(throwaway_name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[throwaway_name] = module
    spec.loader.exec_module(module)
    return module


def _membership(wom_id: int, display_name: str, ehb):
    """A stand-in for wom.GroupMembership: .player_id + a full .player object."""
    player = types.SimpleNamespace(
        display_name=display_name, ehb=ehb, ehp=0.0, latest_snapshot=None,
    )
    return types.SimpleNamespace(player_id=wom_id, player=player)


class _OkResult:
    is_ok = True

    def __init__(self, value):
        self._value = value

    def unwrap(self):
        return self._value


@pytest.fixture
def wom_env():
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
        player_name = Column(String(20))
        account_hash = Column(String(100))
        total_level = Column(Integer)
        log_slots = Column(Integer)
        ehb = Column(Float, nullable=True)

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    scoped = scoped_session(Session)

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
    db_mod.NpcList = type("NpcList", (), {})

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

    wiseoldman = _load_real_module("_ehbtest_wiseoldman", "utils/wiseoldman.py")

    # Never touch the network / the shared rate limiter.
    class _AlwaysAllow:
        async def wait(self):
            return True

    wiseoldman.limiter = _AlwaysAllow()

    memberships: list = []

    class _FakeGroups:
        async def get_details(self, group_id):
            return _OkResult(types.SimpleNamespace(
                memberships=list(memberships),
                name="Test Clan",
                member_count=len(memberships),
                group=None,
            ))

    class _FakeClient:
        groups = _FakeGroups()

        async def start(self):
            return None

    wiseoldman.client = _FakeClient()

    env = types.SimpleNamespace(
        wiseoldman=wiseoldman, scoped=scoped, Player=Player, Group=Group,
        memberships=memberships, Session=Session,
    )
    try:
        yield env
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_ehbtest_wiseoldman", None)
        scoped.remove()
        engine.dispose()
        os.remove(db_path)


def _sync(env, **kwargs):
    s = env.Session()
    try:
        result = asyncio.run(env.wiseoldman.fetch_group_members(
            WOM_GROUP_ID, session_to_use=s, force_refresh=True, **kwargs))
    finally:
        s.close()
    return result


def _seed(env, **kw):
    s = env.Session()
    s.add(env.Player(**kw))
    s.commit()
    s.close()


def _read(env, wom_id):
    s = env.Session()
    try:
        return s.query(env.Player).filter(env.Player.wom_id == wom_id).first()
    finally:
        s.close()


# ── the refresh itself ───────────────────────────────────────────────────────

def test_sync_writes_ehb_for_existing_members(wom_env):
    _seed(wom_env, player_id=1, wom_id=111, player_name="Alpha", ehb=None)
    _seed(wom_env, player_id=2, wom_id=222, player_name="Beta", ehb=10.0)
    wom_env.memberships.extend([
        _membership(111, "Alpha", 512.345),
        _membership(222, "Beta", 87.5),
    ])

    ids = _sync(wom_env)

    assert sorted(ids) == [111, 222]
    assert _read(wom_env, 111).ehb == 512.35   # rounded to 2dp
    assert _read(wom_env, 222).ehb == 87.5


def test_unknown_ehb_never_overwrites_stored_value(wom_env):
    _seed(wom_env, player_id=1, wom_id=111, player_name="Alpha", ehb=300.0)
    wom_env.memberships.append(_membership(111, "Alpha", None))

    _sync(wom_env)

    assert _read(wom_env, 111).ehb == 300.0


def test_zero_ehb_is_stored_as_a_real_value(wom_env):
    # A bossless account genuinely has 0.0 — distinct from "unknown".
    _seed(wom_env, player_id=1, wom_id=111, player_name="Alpha", ehb=None)
    wom_env.memberships.append(_membership(111, "Alpha", 0.0))

    _sync(wom_env)

    assert _read(wom_env, 111).ehb == 0.0


def test_name_change_still_applied_alongside_ehb(wom_env):
    # The pre-existing name-correction behaviour must survive the addition.
    _seed(wom_env, player_id=1, wom_id=111, player_name="OldName", ehb=None)
    wom_env.memberships.append(_membership(111, "NewName", 42.0))

    _sync(wom_env)

    row = _read(wom_env, 111)
    assert row.player_name == "NewName"
    assert row.ehb == 42.0


def test_membership_list_survives_an_ehb_commit_failure(wom_env):
    """EHB is a side effect: the caller's add/remove pass depends on the member
    list, so a failed EHB commit must not cost it."""
    _seed(wom_env, player_id=1, wom_id=111, player_name="Alpha", ehb=None)
    wom_env.memberships.append(_membership(111, "Alpha", 5.0))

    s = wom_env.Session()
    original_commit = s.commit
    calls = {"n": 0}

    def exploding_commit():
        calls["n"] += 1
        raise RuntimeError("db down")

    s.commit = exploding_commit
    try:
        ids = asyncio.run(wom_env.wiseoldman.fetch_group_members(
            WOM_GROUP_ID, session_to_use=s, force_refresh=True))
    finally:
        s.commit = original_commit
        s.close()

    assert ids == [111]
    assert calls["n"] >= 1


def test_no_ehb_changes_means_no_extra_commit(wom_env):
    """Unchanged EHB must not commit — the sync runs hourly across every clan."""
    _seed(wom_env, player_id=1, wom_id=111, player_name="Alpha", ehb=64.0)
    wom_env.memberships.append(_membership(111, "Alpha", 64.0))

    s = wom_env.Session()
    commits = {"n": 0}
    original_commit = s.commit

    def counting_commit():
        commits["n"] += 1
        return original_commit()

    s.commit = counting_commit
    try:
        asyncio.run(wom_env.wiseoldman.fetch_group_members(
            WOM_GROUP_ID, session_to_use=s, force_refresh=True))
    finally:
        s.commit = original_commit
        s.close()

    assert commits["n"] == 0


def test_one_commit_for_the_whole_clan(wom_env):
    """Batched, not per member (239 clans x N members every hour)."""
    for i in range(5):
        _seed(wom_env, player_id=i + 1, wom_id=100 + i,
              player_name=f"P{i}", ehb=None)
        wom_env.memberships.append(_membership(100 + i, f"P{i}", float(i + 1)))

    s = wom_env.Session()
    commits = {"n": 0}
    original_commit = s.commit

    def counting_commit():
        commits["n"] += 1
        return original_commit()

    s.commit = counting_commit
    try:
        asyncio.run(wom_env.wiseoldman.fetch_group_members(
            WOM_GROUP_ID, session_to_use=s, force_refresh=True))
    finally:
        s.commit = original_commit
        s.close()

    assert commits["n"] == 1


def test_provisioned_stub_player_gets_ehb(wom_env):
    wom_env.memberships.append(_membership(999, "Newcomer", 21.5))

    _sync(wom_env, provision_missing=True)

    row = _read(wom_env, 999)
    assert row is not None
    assert row.account_hash == "wom_temp_999"
    assert row.ehb == 21.5
