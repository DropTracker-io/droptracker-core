"""services/auto_nickname: who gets renamed where after /claim-rsn.

The opt-in key must read as off for a group with no row, only opted-in
servers the claimer is actually in are touched, and one server refusing the
rename (owner, hierarchy, missing permission) never stops the others.
"""
import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

from utils import group_config as gc
from web_api.config_registry import get_config_field

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    # conftest stubs the `services` package, so load the real module by path.
    name = "_auto_nickname_ut"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, "services", "auto_nickname.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


an = _load()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_registry_default_is_off():
    field = get_config_field(gc.AUTO_SET_MEMBER_NICKNAMES)
    assert field is not None and field["type"] == "boolean"
    assert field["default"] is False
    assert gc.absent_default(gc.AUTO_SET_MEMBER_NICKNAMES) == "false"


def test_nickname_for():
    assert an.nickname_for("  Zezima ") == "Zezima"
    assert an.nickname_for("") is None
    assert an.nickname_for(None) is None
    assert len(an.nickname_for("x" * 40)) == an.MAX_NICKNAME_LENGTH


def test_select_target_guilds_filters_and_dedupes():
    rows = [
        (10, "111", "true"),
        (11, "222", "1"),
        (12, "333", "false"),     # opted out
        (13, None, "true"),       # no server linked
        (14, "not-a-snowflake", "true"),
        (15, "111", "true"),      # same server as group 10
        (16, " 444 ", " True "),
    ]
    assert an.select_target_guilds(rows) == [(10, 111), (11, 222), (16, 444)]


def _member(nick=None):
    m = MagicMock()
    m.nick = nick
    m.edit_nickname = AsyncMock()
    return m


def _guild(member, owner=False):
    g = MagicMock()
    g.fetch_member = AsyncMock(return_value=member)
    g.is_owner = MagicMock(return_value=owner)
    return g


def _bot(guilds):
    bot = MagicMock()
    bot.fetch_guild = AsyncMock(side_effect=lambda gid: guilds.get(gid))
    return bot


def test_rename_in_guild_outcomes():
    renamed = _member(nick="old")
    assert _run(an._rename_in_guild(_bot({1: _guild(renamed)}), 1, 5, "Zezima")) == "renamed"
    renamed.edit_nickname.assert_awaited_once()
    assert renamed.edit_nickname.await_args.args[0] == "Zezima"

    same = _member(nick="Zezima")
    assert _run(an._rename_in_guild(_bot({1: _guild(same)}), 1, 5, "Zezima")) == "unchanged"
    same.edit_nickname.assert_not_awaited()

    owner = _member()
    assert _run(an._rename_in_guild(_bot({1: _guild(owner, owner=True)}), 1, 5, "Zezima")) == "owner"
    owner.edit_nickname.assert_not_awaited()

    assert _run(an._rename_in_guild(_bot({1: _guild(None)}), 1, 5, "Zezima")) == "not_member"
    assert _run(an._rename_in_guild(_bot({}), 1, 5, "Zezima")) == "no_guild"


def test_apply_continues_past_a_refused_server(monkeypatch):
    refused = _member()
    refused.edit_nickname = AsyncMock(side_effect=RuntimeError("403 Missing Permissions"))
    ok = _member()
    bot = _bot({1: _guild(refused), 2: _guild(None), 3: _guild(ok)})
    monkeypatch.setattr(an, "_load_target_guilds", lambda: [(10, 1), (11, 2), (12, 3)])

    counts = _run(an.apply_rsn_nickname(bot, "5", "Zezima"))

    assert counts == {"failed": 1, "not_member": 1, "renamed": 1}
    ok.edit_nickname.assert_awaited_once()


def test_apply_skips_lookup_without_a_name(monkeypatch):
    lookup = MagicMock()
    monkeypatch.setattr(an, "_load_target_guilds", lookup)
    assert _run(an.apply_rsn_nickname(_bot({}), "5", "  ")) == {}
    lookup.assert_not_called()
