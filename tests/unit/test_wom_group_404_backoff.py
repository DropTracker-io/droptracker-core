"""Dead WOM groups back off instead of being re-fetched every sync round.

WOM flagged ~800 404s/day from us (2026-11-07): 29 linked WOM ids no longer
exist, and the core bot's membership sync fetched each one ~16x/day forever.
``fetch_group_members`` now records consecutive 404s in Redis and skips the
call until the backoff expires; any successful fetch clears it.

Loads the real ``utils/wiseoldman.py`` (conftest stubs it) with fake db/redis
modules, the same way test_session_leak_scoped_cleanup does.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from wom import Err, Ok
from wom.models import HttpErrorResponse

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeRedisConn:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, _ttl, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class _FakeGroups:
    def __init__(self):
        self.calls = 0
        self.result = None

    async def get_details(self, *_a, **_k):
        self.calls += 1
        return self.result


@pytest.fixture
def wom_env(monkeypatch):
    db_mod = types.ModuleType("db")
    models_ns = types.ModuleType("db.models")
    for mod in (db_mod, models_ns):
        mod.Player = type("Player", (), {})
        mod.Group = type("Group", (), {})
        mod.session = None
    db_mod.models = models_ns
    db_mod.NpcList = type("NpcList", (), {})

    redis_stub = types.ModuleType("utils.redis")
    conn = _FakeRedisConn()
    redis_stub.redis_client = types.SimpleNamespace(client=conn)

    saved = {}
    for name, module in (("db", db_mod), ("db.models", models_ns), ("utils.redis", redis_stub)):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    spec = importlib.util.spec_from_file_location(
        "_womtest_wiseoldman", REPO_ROOT / "utils/wiseoldman.py")
    wiseoldman = importlib.util.module_from_spec(spec)
    sys.modules["_womtest_wiseoldman"] = wiseoldman
    spec.loader.exec_module(wiseoldman)

    groups = _FakeGroups()

    async def _ok():
        return True

    async def _noop():
        return None

    monkeypatch.setattr(wiseoldman.limiter, "wait", _ok)
    monkeypatch.setattr(wiseoldman, "client",
                        types.SimpleNamespace(start=_noop, groups=groups))
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(wiseoldman.time, "time", lambda: clock["now"])

    try:
        yield types.SimpleNamespace(wom=wiseoldman, groups=groups, conn=conn, clock=clock)
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_womtest_wiseoldman", None)


def _not_found():
    return Err(HttpErrorResponse(message="Group not found.", status=404))


def _fetch(env, gid=99999):
    # wom_group_id != 1 and force_refresh, exactly like the core member sync.
    return asyncio.run(env.wom.fetch_group_members(gid, force_refresh=True))


def test_404_starts_backoff_and_skips_next_round(wom_env):
    wom_env.groups.result = _not_found()

    assert _fetch(wom_env) == []
    assert wom_env.groups.calls == 1

    # Next sync round (90 min later) is inside the 6h backoff: no WOM call.
    wom_env.clock["now"] += 90 * 60
    assert _fetch(wom_env) == []
    assert wom_env.groups.calls == 1


def test_backoff_doubles_per_consecutive_404_and_caps(wom_env):
    wom = wom_env.wom
    wom_env.groups.result = _not_found()

    _fetch(wom_env)
    wom_env.clock["now"] += wom.GROUP_404_BACKOFF_BASE + 1
    _fetch(wom_env)
    assert wom_env.groups.calls == 2

    # Second 404 waits twice as long: the first base interval is not enough.
    wom_env.clock["now"] += wom.GROUP_404_BACKOFF_BASE + 1
    _fetch(wom_env)
    assert wom_env.groups.calls == 2

    assert wom._group_404_backoff(1) == wom.GROUP_404_BACKOFF_BASE
    assert wom._group_404_backoff(2) == wom.GROUP_404_BACKOFF_BASE * 2
    assert wom._group_404_backoff(50) == wom.GROUP_404_BACKOFF_MAX


def test_other_errors_do_not_back_off(wom_env):
    wom_env.groups.result = Err(HttpErrorResponse(message="Too many requests", status=429))

    _fetch(wom_env)
    _fetch(wom_env)
    assert wom_env.groups.calls == 2
    assert not wom_env.conn.store


def test_successful_group_check_clears_backoff(wom_env):
    wom = wom_env.wom
    wom_env.groups.result = _not_found()
    _fetch(wom_env, gid=4242)
    assert wom._group_in_404_backoff(4242)

    # An admin re-links / checks the id from the site and it exists now.
    details = types.SimpleNamespace(
        memberships=[], group=types.SimpleNamespace(name="Back", member_count=0),
        name="Back", member_count=0)
    wom_env.groups.result = Ok(details)
    asyncio.run(wom.check_group_by_id(4242))

    assert not wom._group_in_404_backoff(4242)
