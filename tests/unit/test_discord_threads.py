"""Reopening auto-archived threads before editing into them (utils/discord_threads).

Boards the bot edits in place (Hall of Fame, lootboard, event standings, Clan
Log) freeze once Discord archives the thread they live in, because an edit is
not activity. These pin the decisions: only threads are touched, an archived
thread is reopened, and a locked one we may not reopen is recorded for the
group's Diagnostics page instead of failing silently forever.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import utils.discord_threads as dt


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class Forbidden403(Exception):
    status = 403
    code = 50013


class FakeHttp:
    def __init__(self, thread, reopen_error=None):
        self.thread = thread
        self.reopen_error = reopen_error
        self.gets = 0
        self.patches = []

    async def get_channel(self, channel_id):
        self.gets += 1
        return self.thread

    async def modify_channel(self, channel_id, data, reason=None):
        if self.reopen_error:
            raise self.reopen_error
        self.patches.append((channel_id, data))
        return {**self.thread, "thread_metadata": {**self.thread["thread_metadata"], "archived": False}}


def thread(archived=True, locked=False, kind=11):
    return {
        "id": "55", "type": kind, "name": "Hall of Fame", "parent_id": "9",
        "thread_metadata": {"archived": archived, "locked": locked, "auto_archive_duration": 4320},
    }


@pytest.fixture()
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(dt, "_redis", lambda: fake)
    return fake


def run(http, channel, **kw):
    return asyncio.run(dt.ensure_thread_open(http, channel, **kw))


def test_ordinary_channels_cost_nothing(redis):
    http = FakeHttp(thread())
    assert run(http, SimpleNamespace(id=1, type=0), group_id=7, feature="lootboard") == dt.NOT_THREAD
    assert http.gets == 0 and not redis.store


def test_archived_thread_is_reopened_silently(redis):
    http = FakeHttp(thread(archived=True))
    state = run(http, SimpleNamespace(id=55, type=11), group_id=7, feature="hall_of_fame")
    assert state == dt.REOPENED
    assert http.patches == [(55, {"archived": False})]
    record = json.loads(redis.store["thread_health:7:hall_of_fame"])
    assert record["state"] == dt.REOPENED and record["thread_name"] == "Hall of Fame"


def test_open_thread_is_left_alone(redis):
    http = FakeHttp(thread(archived=False))
    assert run(http, SimpleNamespace(id=55, type=11), group_id=7, feature="lootboard") == dt.OPEN
    assert http.patches == []


def test_locked_thread_without_manage_threads_is_reported(redis):
    http = FakeHttp(thread(archived=True, locked=True), reopen_error=Forbidden403("Missing Permissions"))
    assert run(http, SimpleNamespace(id=55, type=11), group_id=7, feature="lootboard") == dt.LOCKED
    health = dt.group_thread_health(7)
    assert health[0]["problem"] and health[0]["label"] == "Loot leaderboard"
    assert dt.LOCKED in dt.PROBLEM_STATES


def test_problem_clears_once_the_thread_is_open_again(redis):
    locked = FakeHttp(thread(archived=True, locked=True), reopen_error=Forbidden403("Missing Permissions"))
    run(locked, SimpleNamespace(id=55, type=11), group_id=7, feature="lootboard")
    assert dt.group_thread_health(7)
    run(FakeHttp(thread(archived=False)), SimpleNamespace(id=55, type=11), group_id=7, feature="lootboard")
    assert dt.group_thread_health(7) == []


def test_transient_failures_record_nothing_and_never_raise(redis):
    class Boom(Exception):
        status = 500

    http = FakeHttp(thread(archived=True), reopen_error=Boom("server error"))
    assert run(http, SimpleNamespace(id=55, type=11), group_id=7, feature="clan_log") == dt.ERROR
    assert not redis.store
