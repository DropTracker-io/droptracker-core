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
def wom_module():
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
        yield _load_real_module("_real_wiseoldman_degenerate", "utils/wiseoldman.py")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_real_wiseoldman_degenerate", None)


@pytest.fixture
def is_degenerate(wom_module):
    return wom_module._is_degenerate_wom_player


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


# ── check_user_by_username: a placeholder is a reason to ask WOM, not to refuse ──
#
# Refusing a placeholder outright locked real players out indefinitely: nothing
# ever asked WOM to scrape the name again, and our own update_player mints such
# records whenever a new account's first scrape fails (SpoonedButy, 2026-09-08:
# 404 -> update_player fails -> every later get_details returns the empty
# record). A player with no local row then had every submission discarded until
# someone refreshed the name on wiseoldman.net by hand -- 1-19 (ticket #434)
# lost 73 submissions that way, and 46 names hit the guard in nine days.

from wom import Err, Ok

PLACEHOLDER_1_19 = types.SimpleNamespace(
    id=3305763, username="1 19", display_name="1 19", exp=0, last_changed_at=None,
)
SCRAPED_1_19 = types.SimpleNamespace(
    id=3305763, username="1 19", display_name="1 19", exp=76290175,
    last_changed_at="2026-09-13T12:20:52.029Z",
)
FAILED = (None, None, None, -1)


class _FakePlayers:
    def __init__(self, details, update=None):
        self._details = details
        self._update = update
        self.update_calls = []

    async def get_details(self, username):
        return self._details

    async def update_player(self, username):
        self.update_calls.append(username)
        assert self._update is not None, "update_player must not be called here"
        if callable(self._update):  # a coroutine function standing in for a slow WOM
            return await self._update()
        return self._update


@pytest.fixture
def lookup(wom_module, monkeypatch):
    """Wire a fake WOM client in; returns (check_user_by_username, install)."""

    class _OpenLimiter:
        async def wait(self):
            return True

    class _Client:
        players = None

        async def start(self):
            return None

    client = _Client()
    monkeypatch.setattr(wom_module, "limiter", _OpenLimiter())
    monkeypatch.setattr(wom_module, "client", client)

    def install(details, update=None):
        client.players = _FakePlayers(details, update)
        return client.players

    return wom_module.check_user_by_username, install


def _http_error(status, message):
    return Err(types.SimpleNamespace(status=status, message=message))


class TestPlaceholderLookupAsksWomToScrape:
    async def test_placeholder_that_scrapes_becomes_identity(self, lookup):
        check, install = lookup
        players = install(Ok(PLACEHOLDER_1_19), update=Ok(SCRAPED_1_19))

        _identity, name, wom_id, _slots = await check("1-19")

        assert players.update_calls == ["1-19"]
        assert (name, int(wom_id)) == ("1 19", 3305763)

    async def test_placeholder_that_cannot_scrape_still_fails(self, lookup):
        # "unknown" (796802) is not on the hiscores, so WOM refuses to update it:
        # the corruption this guard exists for must stay impossible.
        check, install = lookup
        players = install(
            Ok(types.SimpleNamespace(id=796802, username="unknown", display_name="unknown",
                                     exp=0, last_changed_at=None)),
            update=_http_error(400, "Failed to load hiscores: Invalid username."),
        )

        assert await check("Unknown") == FAILED
        assert players.update_calls == ["Unknown"]

    async def test_placeholder_that_updates_to_another_placeholder_still_fails(self, lookup):
        check, install = lookup
        install(Ok(PLACEHOLDER_1_19), update=Ok(PLACEHOLDER_1_19))

        assert await check("1-19") == FAILED

    async def test_update_that_raises_fails_closed(self, lookup):
        check, install = lookup

        class _Boom:
            @property
            def is_ok(self):
                raise RuntimeError("connection reset")

        install(Ok(PLACEHOLDER_1_19), update=_Boom())

        assert await check("1-19") == FAILED

    async def test_update_that_hangs_is_abandoned_quickly(self, lookup, wom_module, monkeypatch):
        # WOM answered Le Baronator's update after 125s with a non-JSON error
        # page, holding a webhook-consumer worker the whole time. Stop waiting;
        # WOM still saves the scrape, and the next lookup finds it.
        import asyncio
        import time

        check, install = lookup
        state = {"cancelled": False}

        async def hanging_wom():
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return Ok(SCRAPED_1_19)

        players = install(Ok(PLACEHOLDER_1_19), update=hanging_wom)
        monkeypatch.setattr(wom_module, "WOM_UPDATE_TIMEOUT_SECONDS", 0.05)

        started = time.monotonic()
        assert await check("Le Baronator") == FAILED
        assert time.monotonic() - started < 1
        assert players.update_calls == ["Le Baronator"]
        assert state["cancelled"]

    async def test_scraped_record_costs_no_update_call(self, lookup):
        check, install = lookup
        players = install(Ok(SCRAPED_1_19))  # update_player would assert

        _identity, name, wom_id, _slots = await check("1-19")

        assert players.update_calls == []
        assert (name, int(wom_id)) == ("1 19", 3305763)

    async def test_rate_limited_lookup_is_not_escalated(self, lookup):
        # Escalating on any failure doubled WOM traffic exactly when WOM was
        # already struggling; only 404 and placeholders earn an update call.
        check, install = lookup
        players = install(_http_error(429, "Too many requests"))

        assert await check("1-19") == FAILED
        assert players.update_calls == []

    async def test_not_found_is_still_escalated(self, lookup):
        check, install = lookup
        players = install(_http_error(404, "Player not found."), update=Ok(SCRAPED_1_19))

        _identity, name, _wom_id, _slots = await check("1-19")

        assert players.update_calls == ["1-19"]
        assert name == "1 19"
