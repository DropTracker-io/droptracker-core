"""A plugin-authed row keeps its own WOM record when its new name resolves to
another account's (ticket 550, 2026-11-25).

A main and its alt swapped names. On each rename the submitted name still
resolved, on WOM, to the record of the name's previous holder, and
``ensure_player_and_auth`` re-pinned the row to it. The main's borrowed record
was then archived (404), and the clan sync, which matches members by
``players.wom_id``, evicted the main from its clan.

Two pieces are locked in here:
  * ``utils.wiseoldman.wom_id_status`` only says "gone" when WOM positively
    says so (404 or the empty placeholder record); every failure is "unknown".
  * ``data.submissions.common._hash_row_keeps_its_wom_record`` keeps the held id
    unless that id is confirmed gone.

conftest stubs ``utils.wiseoldman``, so the real module is loaded by file path
(as test_wom_group_sync_ehb.py does) with Redis, the client and the limiter faked.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── wom_id_status ────────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


class _Result:
    def __init__(self, ok, value=None, status=None):
        self.is_ok = ok
        self._value = value
        self._status = status

    def unwrap(self):
        return self._value

    def unwrap_err(self):
        return types.SimpleNamespace(status=self._status)


@pytest.fixture
def wom():
    db_mod = types.ModuleType("db")
    db_mod.Player = type("Player", (), {})
    db_mod.session = None
    db_mod.models = types.ModuleType("db.models")
    db_mod.NpcList = type("NpcList", (), {})
    redis_stub = types.ModuleType("utils.redis")
    fake_redis = _FakeRedis()
    redis_stub.redis_client = types.SimpleNamespace(client=fake_redis)

    saved = {}
    for name, module in (("db", db_mod), ("db.models", db_mod.models),
                         ("utils.redis", redis_stub)):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    spec = importlib.util.spec_from_file_location(
        "_swaptest_wiseoldman", REPO_ROOT / "utils/wiseoldman.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_swaptest_wiseoldman"] = module
    try:
        spec.loader.exec_module(module)

        class _Allow:
            async def wait(self):
                return True

        module.limiter = _Allow()
        players = types.SimpleNamespace(get_details_by_id=AsyncMock())
        module.client = types.SimpleNamespace(start=AsyncMock(), players=players)
        yield types.SimpleNamespace(mod=module, players=players, redis=fake_redis)
    finally:
        for name, module_ in saved.items():
            if module_ is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module_
        sys.modules.pop("_swaptest_wiseoldman", None)


def _status(env, uid=1400078):
    return asyncio.run(env.mod.wom_id_status(uid))


def test_404_is_gone(wom):
    wom.players.get_details_by_id.return_value = _Result(False, status=404)
    assert _status(wom) == "gone"


def test_live_record_is_alive(wom):
    wom.players.get_details_by_id.return_value = _Result(
        True, types.SimpleNamespace(exp=433_670_723, last_changed_at="2026-11-25"))
    assert _status(wom) == "alive"


def test_empty_placeholder_record_is_gone(wom):
    wom.players.get_details_by_id.return_value = _Result(
        True, types.SimpleNamespace(exp=0, last_changed_at=None))
    assert _status(wom) == "gone"


@pytest.mark.parametrize("result", [
    _Result(False, status=500),
    _Result(False, status=429),
    _Result(True, None),
])
def test_anything_unconfirmed_is_unknown(wom, result):
    wom.players.get_details_by_id.return_value = result
    assert _status(wom) == "unknown"


def test_exception_is_unknown(wom):
    wom.players.get_details_by_id.side_effect = RuntimeError("timeout")
    assert _status(wom) == "unknown"


def test_rate_limiter_refusal_is_unknown_and_makes_no_call(wom):
    class _Deny:
        async def wait(self):
            return False

    wom.mod.limiter = _Deny()
    assert _status(wom) == "unknown"
    wom.players.get_details_by_id.assert_not_called()


def test_confirmed_answers_are_cached_unknown_is_not(wom):
    wom.players.get_details_by_id.return_value = _Result(False, status=404)
    assert _status(wom, 1) == "gone"
    assert _status(wom, 1) == "gone"
    assert wom.players.get_details_by_id.await_count == 1

    wom.players.get_details_by_id.reset_mock()
    wom.players.get_details_by_id.return_value = _Result(False, status=503)
    assert _status(wom, 2) == "unknown"
    assert _status(wom, 2) == "unknown"
    assert wom.players.get_details_by_id.await_count == 2


def test_bad_id_is_unknown_without_a_call(wom):
    assert _status(wom, "not-an-id") == "unknown"
    wom.players.get_details_by_id.assert_not_called()


# ── _hash_row_keeps_its_wom_record ───────────────────────────────────────────

def _row(wom_id, account_hash="7504406402720382739"):
    return types.SimpleNamespace(player_id=5773017, wom_id=wom_id, account_hash=account_hash)


@pytest.fixture
def keeps(monkeypatch):
    import data.submissions.common as common

    status = AsyncMock(return_value="alive")
    monkeypatch.setattr(common, "wom_id_status", status)

    def run(row, resolved):
        return asyncio.run(common._hash_row_keeps_its_wom_record(row, resolved))

    return types.SimpleNamespace(run=run, status=status)


def test_live_held_record_is_kept_when_the_name_points_elsewhere(keeps):
    # The ticket-550 shape: the main renamed to the alt's old name, which WOM
    # still maps to the alt's record.
    assert keeps.run(_row(1400078), 3537792) is True
    keeps.status.assert_awaited_once_with(1400078)


def test_unknown_status_keeps_the_held_record(keeps):
    keeps.status.return_value = "unknown"
    assert keeps.run(_row(1400078), 3537792) is True


def test_confirmed_dead_held_record_is_given_up(keeps):
    keeps.status.return_value = "gone"
    assert keeps.run(_row(3537792), 1400078) is False


@pytest.mark.parametrize("row,resolved", [
    (None, 1400078),                                   # no hash owner
    (_row(1400078), None),                             # WOM gave no id
    (_row(1400078), 1400078),                          # nothing changed
    (_row(None), 1400078),                             # row has no id to keep
    (_row(1400078, "wom_temp_1400078"), 3537792),      # stubs never anchor an identity
])
def test_no_lookup_when_there_is_nothing_to_decide(keeps, row, resolved):
    assert keeps.run(row, resolved) is False
    keeps.status.assert_not_awaited()


def test_ensure_player_and_auth_consults_the_guard():
    """The guard must sit in front of the re-pinning: it has to run before the
    `player_by_wom` lookup that would otherwise redirect to the other account."""
    src = (REPO_ROOT / "data/submissions/common.py").read_text(encoding="utf-8")
    start = src.index("async def ensure_player_and_auth(")
    body = src[start:src.index("\nunique_id_cache", start)]
    guard = body.index("_hash_row_keeps_its_wom_record(")
    redirect = body.index("player_by_wom = session.query(Player)")
    assert guard < redirect
