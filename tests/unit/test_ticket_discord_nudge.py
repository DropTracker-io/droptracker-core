"""Support tickets and the main Discord server: the join nudge and late joiners.

* **Unknown is not "not a member".** The site shows "join our Discord" only on
  a definite False. A stale member cache (the webhook bot down, role sync off)
  or a user with no linked Discord id answers None, so nobody already in the
  server is nagged on a guess.
* **A full listing replaces the set; join/leave events patch it.** An empty
  listing is a fetch problem and never wipes the cache.
* **A late joiner is added once, and a staff deny is never overridden.** The
  grant only happens when the channel has no overwrite for the opener at all.

services/ticket_system.py is loaded from its file path as in
test_ticket_autoclose.py.
"""
import asyncio
import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_membership():
    # discord_membership imports services.discord_roles; give it the real
    # module (registered before exec: its dataclasses look themselves up).
    with pytest.MonkeyPatch.context() as mp:
        spec = importlib.util.spec_from_file_location(
            "services.discord_roles", os.path.join(_ROOT, "services", "discord_roles.py"))
        roles = importlib.util.module_from_spec(spec)
        mp.setitem(sys.modules, "services.discord_roles", roles)
        spec.loader.exec_module(roles)
        return _load("_discord_membership_ut", "services/discord_membership.py")


dm = _load_membership()


def _load_ticket_system():
    with pytest.MonkeyPatch.context() as mp:
        for name in ("interactions.api", "interactions.api.events", "interactions.models", "commands"):
            mp.setitem(sys.modules, name, MagicMock())
        return _load("_ticket_system_nudge_ut", "services/ticket_system.py")


ts = _load_ticket_system()


class FakeRedis:
    """The handful of set/string commands discord_membership uses."""

    def __init__(self):
        self.data = {}

    def pipeline(self):
        return _Pipe(self)

    def sadd(self, key, *vals):
        self.data.setdefault(key, set()).update(vals)

    def srem(self, key, *vals):
        self.data.get(key, set()).difference_update(vals)

    def sismember(self, key, val):
        return val in self.data.get(key, set())

    def exists(self, key):
        return int(key in self.data)

    def set(self, key, val, ex=None):
        self.data[key] = val

    def delete(self, key):
        self.data.pop(key, None)

    def rename(self, src, dst):
        self.data[dst] = self.data.pop(src)


class _Pipe:
    def __init__(self, conn):
        self.conn, self.ops = conn, []

    def __getattr__(self, name):
        return lambda *a, **k: self.ops.append((name, a, k))

    def execute(self):
        for name, a, k in self.ops:
            getattr(self.conn, name)(*a, **k)


# --------------------------------------------------------------------------- #
# discord_membership
# --------------------------------------------------------------------------- #
def test_unknown_until_a_full_listing_has_been_stored():
    r = FakeRedis()
    dm.mark_joined("111", conn=r)  # a join alone does not make the set authoritative
    assert dm.is_member("111", conn=r) is None


def test_full_listing_answers_true_and_false():
    r = FakeRedis()
    assert dm.replace_members(["111", 222], conn=r) == 2
    assert dm.is_member("111", conn=r) is True
    assert dm.is_member(222, conn=r) is True
    assert dm.is_member("333", conn=r) is False


def test_join_and_leave_patch_the_set():
    r = FakeRedis()
    dm.replace_members(["111"], conn=r)
    dm.mark_joined("333", conn=r)
    assert dm.is_member("333", conn=r) is True
    dm.mark_left("111", conn=r)
    assert dm.is_member("111", conn=r) is False


def test_replace_drops_people_who_left():
    r = FakeRedis()
    dm.replace_members(["111", "222"], conn=r)
    dm.replace_members(["222"], conn=r)
    assert dm.is_member("111", conn=r) is False


def test_an_empty_listing_never_wipes_the_cache():
    r = FakeRedis()
    dm.replace_members(["111"], conn=r)
    assert dm.replace_members([], conn=r) == 0
    assert dm.is_member("111", conn=r) is True


def test_stale_cache_is_unknown():
    r = FakeRedis()
    dm.replace_members(["111"], conn=r)
    r.delete(dm.FRESH_KEY)  # TTL expired
    assert dm.is_member("111", conn=r) is None


@pytest.mark.parametrize("discord_id", [None, "", "0", "abc"])
def test_no_linked_discord_is_unknown(discord_id):
    r = FakeRedis()
    dm.replace_members(["111"], conn=r)
    assert dm.is_member(discord_id, conn=r) is None


def test_ids_from_raw_member_objects():
    members = [{"user": {"id": "1"}}, {"user": {"id": 2}}, {"nick": "no user"}, None]
    assert dm.ids_from_member_objects(members) == ["1", "2"]


def test_redis_errors_degrade_to_unknown():
    conn = MagicMock()
    conn.exists.side_effect = ConnectionError("down")
    assert dm.is_member("111", conn=conn) is None


# --------------------------------------------------------------------------- #
# Late joiners: ticket_system
# --------------------------------------------------------------------------- #
def _channel(overwrite_ids=()):
    return SimpleNamespace(
        permission_overwrites=[SimpleNamespace(id=i) for i in overwrite_ids],
        add_permission=AsyncMock(),
        send=AsyncMock(),
    )


def _bot(channel):
    return SimpleNamespace(fetch_channel=AsyncMock(return_value=channel))


def _grant(bot, *args):
    return asyncio.run(ts.grant_opener_access(bot, *args))


def test_member_overwrite_detection():
    assert ts._has_member_overwrite(_channel([555]), "555") is True
    assert ts._has_member_overwrite(_channel([999]), "555") is False
    assert ts._has_member_overwrite(SimpleNamespace(), "555") is False


def test_late_joiner_is_granted_and_announced():
    channel = _channel([999])
    bot = _bot(channel)
    granted = _grant(bot, 7, "123", "555")
    assert granted is True
    channel.add_permission.assert_awaited_once()
    assert channel.add_permission.await_args.kwargs["allow"] == ts.OPENER_PERMISSIONS
    channel.send.assert_awaited_once()
    assert "<@555>" in channel.send.await_args.args[0]


def test_existing_overwrite_is_left_alone():
    """Already added, or denied by staff: no second grant, no message."""
    channel = _channel([555])
    bot = _bot(channel)
    assert _grant(bot, 7, "123", "555") is False
    channel.add_permission.assert_not_awaited()
    channel.send.assert_not_awaited()


def test_missing_channel_is_skipped():
    bot = _bot(None)
    assert _grant(bot, 7, "123", "555") is False


# --------------------------------------------------------------------------- #
# web_api: viewer_discord_member never leaks a non-bool
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("answer", [True, False, None])
def test_viewer_discord_member_passes_bools_and_none(monkeypatch, answer):
    from web_api.routes import tickets as routes

    monkeypatch.setitem(sys.modules, "services.discord_membership",
                        SimpleNamespace(is_member=lambda _id: answer))
    assert routes.viewer_discord_member(SimpleNamespace(discord_id="1")) is answer


def test_viewer_discord_member_coerces_junk_to_none(monkeypatch):
    from web_api.routes import tickets as routes

    monkeypatch.setitem(sys.modules, "services.discord_membership",
                        SimpleNamespace(is_member=lambda _id: MagicMock()))
    assert routes.viewer_discord_member(SimpleNamespace(discord_id="1")) is None
