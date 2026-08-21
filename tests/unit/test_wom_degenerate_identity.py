"""WOM placeholder records must never become authoritative identity.

WOM hands out rows for names it has registered but never successfully scraped:
0 exp, combat 3, ``type`` "unknown", no ``last_changed_at``. Id 796802 is one of
them and its ``displayName`` is the literal string "unknown".

On 2026-07-27 one of these was adopted by a real, plugin-authed player row. The
row was renamed to "unknown" and repinned to 796802, which stopped the clan
roster matching it by id (so a wom_temp stub was minted and absorbed its clans
and its bingo signup) and made the submitted RSN resolve to that stub — after
which ``check_auth`` refused every submission. Roughly three weeks of drops,
collection log entries, combat achievements and personal bests were discarded
without a single error being raised.

Like test_wom_group_sync_ehb.py, the conftest stubs ``utils.wiseoldman`` with a
MagicMock, so the real module is loaded by file path.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_real_module(throwaway_name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(throwaway_name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[throwaway_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def is_degenerate():
    """Load the real module over the conftest stubs, then put them back.

    The conftest replaces ``utils.wiseoldman`` (and the ``db`` / ``utils.redis``
    modules it imports) with MagicMocks, so the real file has to be loaded by
    path with importable stand-ins in place — the same dance as
    test_wom_group_sync_ehb.py. Restoring in a finally keeps the swap from
    leaking into whatever test runs next; without it, ordering decides whether
    this file passes.
    """
    db_mod = types.ModuleType("db")
    models_ns = types.ModuleType("db.models")
    models_ns.Player = type("Player", (), {})
    models_ns.Group = type("Group", (), {})
    models_ns.NpcList = type("NpcList", (), {})
    db_mod.models = models_ns
    db_mod.NpcList = models_ns.NpcList
    # utils/wiseoldman.py does `from db import Player, session, models`.
    db_mod.Player = models_ns.Player
    db_mod.session = None

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

    try:
        mod = _load_real_module("_real_wiseoldman_degenerate", "utils/wiseoldman.py")
        yield mod._is_degenerate_wom_player
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_real_wiseoldman_degenerate", None)


def _record(exp, last_changed_at):
    return types.SimpleNamespace(exp=exp, last_changed_at=last_changed_at)


class TestIsDegenerateWomPlayer:
    def test_the_796802_placeholder_is_degenerate(self, is_degenerate):
        # The exact shape WOM returns for id 796802 / displayName "unknown".
        assert is_degenerate(_record(0, None)) is True

    def test_live_account_is_not_degenerate(self, is_degenerate):
        # Wimi's real record: 346M exp, tracked daily.
        assert is_degenerate(_record(346636278, "2026-08-19T12:14:26.592Z")) is False

    def test_low_exp_but_tracked_is_not_degenerate(self, is_degenerate):
        # A real new account still has starting-stat exp and a change timestamp;
        # the guard must not strand genuine low-level players.
        assert is_degenerate(_record(1154, "2026-08-19T00:00:00.000Z")) is False

    def test_exp_alone_does_not_condemn_a_tracked_account(self, is_degenerate):
        # exp 0 *with* a real snapshot history is not a placeholder.
        assert is_degenerate(_record(0, "2026-08-19T00:00:00.000Z")) is False

    def test_missing_change_timestamp_alone_is_not_enough(self, is_degenerate):
        assert is_degenerate(_record(346636278, None)) is False

    def test_missing_exp_attribute_is_not_degenerate(self, is_degenerate):
        # Unknown shape: fail open rather than reject a real identity.
        assert is_degenerate(types.SimpleNamespace()) is False

    def test_unparseable_exp_is_not_degenerate(self, is_degenerate):
        assert is_degenerate(_record("not-a-number", None)) is False
