"""A Bug Tester's mirrored submission on the dev instance.

Unlike the firehose, a tester's copy is processed as if it had been sent
straight to dev: their groups, events and points. What keeps that inside dev
is the instance's own guard, so these pin the pieces it rests on:

* the ``X-DT-Mirror`` value is classified conservatively (anything unknown is
  the firehose, which has the sink);
* the consumer processes a tester's copy without the sink, but only on an
  instance whose guild guard is armed;
* on such an instance, notifications for groups outside DEV_ALLOWED_GUILDS
  are never queued — and nothing changes in production;
* the dev bot may DM current testers, and nobody else it does not know;
* the dev guild gets its own role map, and dev serves its own screenshots.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from tests.unit import _tester_db as tdb
from utils import dev_guild_guard as guard
from utils import mirror_context as mc

DEV_GUILD = 1436315863434133598


class TestMirrorKind:
    @pytest.mark.parametrize("value,kind", [
        (None, None), ("", None), ("   ", None),
        ("tester", "tester"), (" Tester ", "tester"),
        ("1", "all"), ("0", "all"), ("banana", "all"),
    ])
    def test_classification(self, value, kind):
        assert mc.mirror_kind(value) == kind


# --------------------------------------------------------------------------- #
# The consumer
# --------------------------------------------------------------------------- #
wc = pytest.importorskip("workers.webhook_consumer")


@pytest.fixture()
def recorded(monkeypatch):
    """Replace the real processing with a recorder of the sink in force."""
    seen = []

    async def fake_process(entry_bytes):
        seen.append({"entry": json.loads(entry_bytes), "sink": mc.sink_group_id()})

    monkeypatch.setattr(wc, "_process_submission", fake_process)
    monkeypatch.setattr(wc, "_resolve_mirror_sink", lambda: 99)
    monkeypatch.setitem(wc._tester_refusal_logged, "done", False)
    return seen


def _run(entry):
    asyncio.run(wc._process_entry(json.dumps(entry).encode()))


@pytest.fixture()
def armed(monkeypatch):
    monkeypatch.setenv("STATE", "dev")
    monkeypatch.setenv("DEV_ALLOWED_GUILDS", str(DEV_GUILD))
    return monkeypatch


class TestConsumerRouting:
    def test_a_testers_copy_is_processed_without_the_sink(self, recorded, armed):
        _run({"payload": {}, "mirrored": "tester"})
        assert recorded == [{"entry": {"payload": {}, "mirrored": "tester"}, "sink": None}]

    def test_the_firehose_goes_to_the_sink(self, recorded, armed):
        _run({"payload": {}, "mirrored": "all"})
        assert recorded[0]["sink"] == 99

    def test_an_entry_from_before_kinds_goes_to_the_sink(self, recorded, armed):
        _run({"payload": {}, "mirrored": True})
        assert recorded[0]["sink"] == 99

    def test_live_traffic_is_untouched(self, recorded, armed):
        _run({"payload": {}})
        assert recorded[0]["sink"] is None

    def test_a_testers_copy_is_refused_where_the_guard_is_not_armed(self, recorded, monkeypatch, tmp_path):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.delenv("DEV_ALLOWED_GUILDS", raising=False)
        stashed = tmp_path / "shot.png"
        stashed.write_bytes(b"png")
        _run({"payload": {}, "mirrored": "tester", "image_tmp_path": str(stashed)})
        assert recorded == []
        assert not stashed.exists(), "the screenshot it brought is cleaned up"

    def test_a_testers_copy_is_refused_outside_dev(self, recorded, monkeypatch):
        monkeypatch.setenv("STATE", "live")
        monkeypatch.delenv("STATUS", raising=False)
        monkeypatch.setenv("DEV_ALLOWED_GUILDS", str(DEV_GUILD))
        _run({"payload": {}, "mirrored": "tester"})
        assert recorded == []


# --------------------------------------------------------------------------- #
# create_notification on a dev instance
# --------------------------------------------------------------------------- #
class _GuildLookup:
    """A session whose group lookup answers one guild id (or raises)."""

    def __init__(self, guild_id=None, fail=False):
        self.guild_id = guild_id
        self.fail = fail
        self.lookups = 0

    def query(self, *a, **k):
        self.lookups += 1
        if self.fail:
            raise RuntimeError("database is down")
        return self

    def filter(self, *a, **k):
        return self

    def scalar(self):
        return self.guild_id


@pytest.fixture()
def common(monkeypatch):
    from data.submissions import common as module

    module._dev_group_guild_cache.clear()
    return module


@pytest.fixture()
def channel_gate(common, monkeypatch):
    seen = {}

    def fake_gate(db_session, group_id, notification_type):
        seen["group_id"] = group_id
        return False

    monkeypatch.setattr(common, "group_has_notification_channel", fake_gate)
    return seen


def _notify(common, group_id, session):
    return asyncio.run(common.create_notification("drop", 2, {}, group_id=group_id,
                                                  existing_session=session))


class TestDevInstanceSkipsRealClans:
    def test_a_real_clans_group_is_not_queued(self, common, channel_gate, armed):
        assert _notify(common, 5, _GuildLookup(guild_id="123456789")) is None
        assert channel_gate == {}, "skipped before any other gate"

    def test_the_bug_testers_group_is_queued_as_normal(self, common, channel_gate, armed):
        _notify(common, 10000001, _GuildLookup(guild_id=str(DEV_GUILD)))
        assert channel_gate["group_id"] == 10000001

    def test_a_group_with_no_guild_has_nowhere_to_post(self, common, armed):
        assert common.dev_instance_skips_group(_GuildLookup(guild_id=None), 5) is True

    def test_a_lookup_failure_falls_through_to_the_send_time_guard(self, common, armed):
        assert common.dev_instance_skips_group(_GuildLookup(fail=True), 5) is False

    def test_the_guild_is_looked_up_once_per_group(self, common, armed):
        session = _GuildLookup(guild_id="123")
        for _ in range(5):
            common.dev_instance_skips_group(session, 5)
        assert session.lookups == 1

    def test_production_never_skips(self, common, channel_gate, monkeypatch):
        monkeypatch.setenv("STATE", "live")
        monkeypatch.delenv("STATUS", raising=False)
        monkeypatch.setenv("DEV_ALLOWED_GUILDS", str(DEV_GUILD))
        session = _GuildLookup(guild_id="123456789")
        _notify(common, 5, session)
        assert channel_gate["group_id"] == 5
        assert session.lookups == 0, "production does not even look"

    def test_an_unconfigured_dev_box_does_not_skip(self, common, channel_gate, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.delenv("DEV_ALLOWED_GUILDS", raising=False)
        _notify(common, 5, _GuildLookup(guild_id="123456789"))
        assert channel_gate["group_id"] == 5

    def test_the_firehose_sink_is_still_allowed(self, common, channel_gate, armed):
        with common.mirror_sink(10000002):
            _notify(common, 5, _GuildLookup(guild_id=str(DEV_GUILD)))
        assert channel_gate["group_id"] == 10000002


# --------------------------------------------------------------------------- #
# Who the dev bot may DM
# --------------------------------------------------------------------------- #
class _Testers:
    def __init__(self, ids, fail=False):
        self.ids = ids
        self.fail = fail
        self.reads = 0

    def smembers(self, key):
        self.reads += 1
        if self.fail:
            raise ConnectionError("redis is down")
        assert key == guard.TESTER_IDS_KEY
        return {str(i).encode() for i in self.ids}


@pytest.fixture()
def testers(monkeypatch):
    def install(ids, fail=False):
        fake = _Testers(ids, fail)
        monkeypatch.setattr(guard, "_redis", lambda: fake)
        monkeypatch.setitem(guard._tester_ids_cache, "expires", 0.0)
        return fake
    return install


class TestTesterDMs:
    def test_a_current_tester_may_be_dmed(self, testers, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.delenv("DEV_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("DEV_ALLOW_TESTER_DMS", raising=False)
        testers([111])
        assert guard.user_allowed(111) is True
        assert guard.user_allowed("111") is True

    def test_anyone_else_may_not(self, testers, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.delenv("DEV_ALLOWED_USERS", raising=False)
        testers([111])
        assert guard.user_allowed(222) is False

    def test_tester_dms_can_be_switched_off(self, testers, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.delenv("DEV_ALLOWED_USERS", raising=False)
        monkeypatch.setenv("DEV_ALLOW_TESTER_DMS", "false")
        testers([111])
        assert guard.user_allowed(111) is False

    def test_the_explicit_allowlist_still_works(self, testers, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.setenv("DEV_ALLOWED_USERS", "333")
        testers([])
        assert guard.user_allowed(333) is True

    def test_redis_trouble_only_ever_blocks(self, testers, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.delenv("DEV_ALLOWED_USERS", raising=False)
        testers([111], fail=True)
        assert guard.user_allowed(111) is False

    def test_the_tester_list_is_read_at_most_every_few_seconds(self, testers, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.delenv("DEV_ALLOWED_USERS", raising=False)
        fake = testers([111])
        for _ in range(10):
            guard.user_allowed(111)
        assert fake.reads == 1

    def test_garbage_ids_are_refused(self, testers, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        testers([111])
        assert guard.user_allowed("not-an-id") is False
        assert guard.user_allowed(None) is False


# --------------------------------------------------------------------------- #
# Dev's own role map and screenshots
# --------------------------------------------------------------------------- #
class TestRoleMapPath:
    def _load(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("DISCORD_ROLE_MAP_PATH", raising=False)
        else:
            monkeypatch.setenv("DISCORD_ROLE_MAP_PATH", value)
        return tdb.load("_discord_roles_path", "services", "discord_roles.py")

    def test_defaults_to_the_main_servers_map(self, monkeypatch):
        module = self._load(monkeypatch, None)
        assert str(module.ROLE_MAP_PATH) == os.path.join(tdb.REPO_ROOT, "data", "discord_roles.json")

    def test_a_relative_override_is_taken_from_the_repo_root(self, monkeypatch):
        module = self._load(monkeypatch, "data/dev/discord_roles.json")
        assert str(module.ROLE_MAP_PATH) == os.path.join(tdb.REPO_ROOT, "data", "dev", "discord_roles.json")

    def test_an_absolute_override_is_used_as_is(self, monkeypatch, tmp_path):
        target = tmp_path / "roles.json"
        module = self._load(monkeypatch, str(target))
        assert str(module.ROLE_MAP_PATH) == str(target)

    def test_a_dev_map_confines_the_sync_to_the_bug_tester_role(self, monkeypatch, tmp_path):
        target = tmp_path / "roles.json"
        target.write_text(json.dumps({"guild_id": "42", "roles": {"bug_tester": "777"}}))
        monkeypatch.setenv("PRIMARY_GUILD_ID", "42")
        module = self._load(monkeypatch, str(target))
        role_map = module.load_role_map()
        assert role_map == {"bug_tester": "777"}
        desired = {"1": {"patron", "bug_tester"}}
        members = [{"user": {"id": "1"}, "roles": []}]
        plan = module.plan_role_changes(desired, members, role_map)
        assert plan.adds == [("1", "bug_tester")], "no tier role is touched on the dev guild"


class TestUploadBaseUrl:
    def _load(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("USER_UPLOAD_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("USER_UPLOAD_BASE_URL", value)
        return tdb.load("_download_base_url", "utils", "download.py")

    def test_production_default(self, monkeypatch):
        module = self._load(monkeypatch, None)
        assert module.USER_UPLOAD_BASE_URL == "https://www.droptracker.io/img/user-upload/"

    @pytest.mark.parametrize("value", [
        "https://dev-api.droptracker.io/img/user-upload",
        "https://dev-api.droptracker.io/img/user-upload/",
        '"https://dev-api.droptracker.io/img/user-upload/"',
    ])
    def test_dev_serves_its_own(self, monkeypatch, value):
        module = self._load(monkeypatch, value)
        assert module.USER_UPLOAD_BASE_URL == "https://dev-api.droptracker.io/img/user-upload/"
