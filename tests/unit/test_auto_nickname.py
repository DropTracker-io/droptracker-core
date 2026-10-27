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


# --------------------------------------------------------------------------- #
# Turning the setting on: flip detection, planning, the backfill itself
# --------------------------------------------------------------------------- #
def test_turned_on_is_strictly_off_to_on():
    assert an.turned_on(None, "true")       # no row = default off
    assert an.turned_on("false", "true")
    assert an.turned_on("0", "1")
    assert not an.turned_on("true", "true")  # re-save is not a flip
    assert not an.turned_on("1", "true")
    assert not an.turned_on("true", "false")
    assert not an.turned_on(None, "false")


def test_plan_backfill_groups_names_per_user_oldest_first():
    rows = [
        ("100", "Main Acct"),
        ("200", "Solo"),
        ("100", "Alt Acct"),
        ("100", "Main Acct"),   # same player via a second association row
        ("bad", "Nobody"),
        ("300", "   "),
    ]
    assert an.plan_backfill(rows) == [(100, ["Main Acct", "Alt Acct"]), (200, ["Solo"])]


def test_rename_member_keeps_a_nickname_that_is_already_one_of_theirs():
    alt = _member(nick="Alt Acct")
    out = _run(an._rename_member(_guild(alt), 100, "Main Acct", keep=["Main Acct", "Alt Acct"]))
    assert out == "unchanged"
    alt.edit_nickname.assert_not_awaited()

    other = _member(nick="Bob")
    out = _run(an._rename_member(_guild(other), 100, "Main Acct", keep=["Main Acct", "Alt Acct"]))
    assert out == "renamed"
    assert other.edit_nickname.await_args.args[0] == "Main Acct"


def _guild_of(members, owner_id=None):
    g = MagicMock()
    g.fetch_member = AsyncMock(side_effect=lambda uid: members.get(uid))
    g.is_owner = MagicMock(side_effect=lambda uid: uid == owner_id)
    return g


def test_backfill_renames_claimed_members_and_survives_failures(monkeypatch):
    monkeypatch.setattr(an, "BACKFILL_PAUSE_SECONDS", 0)
    refused = _member(nick="x")
    refused.edit_nickname = AsyncMock(side_effect=RuntimeError("50013 Missing Permissions"))
    members = {1: _member(nick=None), 2: _member(nick="Two"), 3: refused, 5: _member()}
    guild = _guild_of(members, owner_id=5)
    plan = [(1, ["One"]), (2, ["Two"]), (3, ["Three"]), (4, ["Gone"]), (5, ["Owner"])]
    monkeypatch.setattr(an, "_load_backfill", lambda gid, guild_id: plan)

    counts = _run(an.backfill_group(_bot({777: guild}), 10, 777))

    assert counts == {"renamed": 1, "unchanged": 1, "failed": 1, "not_member": 1, "owner": 1}
    members[1].edit_nickname.assert_awaited_once()
    assert 777 not in an._backfilling


def test_backfill_skips_a_stale_job(monkeypatch):
    # Setting switched back off (or the group moved server) before the drain.
    monkeypatch.setattr(an, "_load_backfill", lambda gid, guild_id: None)
    bot = _bot({})
    assert _run(an.backfill_group(bot, 10, 777)) == {"skipped": 1}
    bot.fetch_guild.assert_not_awaited()


def test_backfill_runs_once_per_guild_at_a_time(monkeypatch):
    an._backfilling.add(777)
    try:
        assert _run(an.backfill_group(_bot({}), 10, 777)) == {"skipped": 1}
    finally:
        an._backfilling.discard(777)


# --------------------------------------------------------------------------- #
# Enqueue side (website / settings panel / claims)
# --------------------------------------------------------------------------- #
def _fake_outbox(monkeypatch):
    outbox = MagicMock()
    monkeypatch.setattr(sys.modules["services"], "discord_outbox", outbox, raising=False)
    return outbox


def _session_with_guild(guild_id):
    s = MagicMock()
    s.query.return_value.filter.return_value.first.return_value = (guild_id,)
    return s


def test_enqueue_backfill_only_on_a_flip(monkeypatch):
    outbox = _fake_outbox(monkeypatch)
    s = _session_with_guild("777")

    assert an.enqueue_backfill_if_turned_on(s, 10, None, "true", actor_user_id=9) is True
    kw = outbox.enqueue.call_args.kwargs
    assert kw["kind"] == an.KIND_BACKFILL and kw["channel_id"] == "777"
    assert kw["ref_id"] == 10 and kw["actor_user_id"] == 9
    # Joins the caller's transaction: the job exists exactly when the change does.
    assert kw["commit"] is False

    outbox.enqueue.reset_mock()
    assert an.enqueue_backfill_if_turned_on(s, 10, "true", "true") is False
    assert an.enqueue_backfill_if_turned_on(s, 10, None, "false") is False
    assert an.enqueue_backfill_if_turned_on(s, an.GLOBAL_GROUP_ID, None, "true") is False
    assert an.enqueue_backfill_if_turned_on(_session_with_guild(None), 10, None, "true") is False
    outbox.enqueue.assert_not_called()


def test_enqueue_claim_rename(monkeypatch):
    outbox = _fake_outbox(monkeypatch)
    assert an.enqueue_claim_rename(MagicMock(), 528746710042804247, " Zezima ") is True
    kw = outbox.enqueue.call_args.kwargs
    assert kw["kind"] == an.KIND_CLAIM
    assert kw["channel_id"] == "528746710042804247" and kw["content"] == "Zezima"

    outbox.enqueue.reset_mock()
    assert an.enqueue_claim_rename(MagicMock(), 5, "") is False
    outbox.enqueue.assert_not_called()


def test_handle_outbox_row_starts_the_right_job(monkeypatch):
    started = []
    monkeypatch.setattr(an, "_spawn", lambda coro: (started.append(coro.cr_code.co_name), coro.close()))

    claim = MagicMock(kind=an.KIND_CLAIM, channel_id="5", content="Zezima")
    backfill = MagicMock(kind=an.KIND_BACKFILL, channel_id="777", ref_id=10)
    an.handle_outbox_row(MagicMock(), claim)
    an.handle_outbox_row(MagicMock(), backfill)
    assert started == ["apply_rsn_nickname", "backfill_group"]

    import pytest
    with pytest.raises(ValueError):
        an.handle_outbox_row(MagicMock(), MagicMock(kind=an.KIND_BACKFILL, channel_id="777", ref_id=None))


def test_outbox_drain_hands_nickname_rows_off(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_outbox_nick_ut", os.path.join(_ROOT, "services", "discord_outbox.py"))
    outbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(outbox)

    class _Col:  # comparison-safe stand-ins (see test_discord_outbox_dm)
        def __lt__(self, other):
            return True

        def __eq__(self, other):
            return True

        def asc(self):
            return self

    class _Outbox:
        created_at = _Col()
        status = _Col()

    monkeypatch.setattr(outbox, "DiscordOutbox", _Outbox)
    handled = []
    fake = MagicMock(handle_outbox_row=lambda bot, row: handled.append(row.kind))
    monkeypatch.setitem(sys.modules, "services.auto_nickname", fake)

    rows = [MagicMock(kind=an.KIND_CLAIM, status="pending"),
            MagicMock(kind=an.KIND_BACKFILL, status="pending")]
    session = MagicMock()
    q = session.query.return_value
    q.filter.return_value.update.return_value = 0
    q.filter.return_value.order_by.return_value.limit.return_value \
        .with_for_update.return_value.all.return_value = rows
    bot = MagicMock()
    bot.fetch_channel = AsyncMock()

    sent = _run(outbox.drain_once(bot, lambda: session, limit=5))

    assert sent == 2 and handled == [an.KIND_CLAIM, an.KIND_BACKFILL]
    assert all(r.status == "sent" for r in rows)
    bot.fetch_channel.assert_not_awaited()  # never treated as a channel send
