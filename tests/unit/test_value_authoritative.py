"""Regression tests: get_true_item_value must not trust a spoofable client value.

Loads the real utils.ge_value in isolation (conftest otherwise stubs it),
stubbing its infra imports so the valuation branch runs without Redis / GE / aiohttp.
"""
import importlib.util
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_real_ge_value(monkeypatch):
    """Import utils/ge_value.py fresh, bypassing the conftest stub.

    Stubs go in via monkeypatch.setitem so they are torn down again: a plain
    ``sys.modules[...] =`` here leaks a Redis-less ``utils.redis`` and a
    one-function ``utils.value_overrides`` into every later test module.
    """
    monkeypatch.setitem(sys.modules, "aiohttp", MagicMock())

    redis_stub = types.ModuleType("utils.redis")
    redis_stub.RedisClient = MagicMock
    monkeypatch.setitem(sys.modules, "utils.redis", redis_stub)

    vo_stub = types.ModuleType("utils.value_overrides")
    vo_stub.match = lambda item_id, item_name: None
    monkeypatch.setitem(sys.modules, "utils.value_overrides", vo_stub)

    path = os.path.join(_REPO_ROOT, "utils", "ge_value.py")
    spec = importlib.util.spec_from_file_location("_real_ge_value_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ge(monkeypatch):
    return _load_real_ge_value(monkeypatch)


@pytest.mark.asyncio
async def test_nonzero_client_value_is_overridden_by_server_price(ge):
    """A huge client value for a priced item is replaced by the real GE price."""
    ge._lookup_and_cache_ge_price = AsyncMock(return_value=1_200_000_000)
    result = await ge.get_true_item_value("Twisted bow", 2_000_000_000, item_id=20997)
    assert result == 1_200_000_000  # server price wins, not the client's 2,000,000,000


@pytest.mark.asyncio
async def test_client_value_used_when_item_has_no_server_price(ge):
    """Untradeables (no GE price) still fall back to the client-reported value."""
    ge._lookup_and_cache_ge_price = AsyncMock(return_value=0)
    result = await ge.get_true_item_value("Some untradeable", 5000, item_id=999999)
    assert result == 5000


@pytest.mark.asyncio
async def test_zero_client_value_still_uses_server_price(ge):
    """Existing behaviour preserved: value=0 recovers the real GE price."""
    ge._lookup_and_cache_ge_price = AsyncMock(return_value=1_200_000_000)
    result = await ge.get_true_item_value("Twisted bow", 0, item_id=20997)
    assert result == 1_200_000_000
