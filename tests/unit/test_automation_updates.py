"""Unit tests for services/automation_updates.py — the Discord reporter for
the GitHub Pages publisher and the WOM fork sync.

Loaded standalone via importlib (conftest stubs services/utils.redis); Discord
and Redis are replaced with in-memory fakes, so these tests pin the refresh
policy: edit the status message in place on a quiet run, delete + repost it
after a change message, repost when a human deleted it.
"""
import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


au = _load("_automation_updates_under_test", "services", "automation_updates.py")


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)


class NotFound(Exception):
    """Name-matched by _is_not_found, like the interactions exception."""


class FakeRest:
    """Stands in for DiscordRest — records calls, supports async-with."""

    instances = []

    def __init__(self, token=None, user_agent=None):
        self.posts = []
        self.edits = []
        self.deletes = []
        self._next_id = 100
        self.edit_raises = None
        self.delete_raises = None
        FakeRest.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post_message(self, channel_id, payload):
        self.posts.append((channel_id, payload))
        self._next_id += 1
        return str(self._next_id)

    async def edit_message(self, channel_id, message_id, payload):
        if self.edit_raises:
            raise self.edit_raises
        self.edits.append((channel_id, message_id, payload))

    async def delete_message(self, channel_id, message_id):
        if self.delete_raises:
            raise self.delete_raises
        self.deletes.append((channel_id, message_id))


class TestParseNextElapse:
    def test_calendar_string_utc(self):
        # systemctl show -p NextElapseUSecRealtime --value output on this box
        epoch = au._parse_next_elapse("Wed 2026-08-05 14:32:23 UTC")
        assert epoch == 1785940343

    def test_inactive_and_garbage(self):
        assert au._parse_next_elapse("n/a") is None
        assert au._parse_next_elapse("") is None
        assert au._parse_next_elapse(None) is None
        assert au._parse_next_elapse("not a timestamp at all") is None


class TestNextRunGithub:
    def test_gate_plus_thirty_minutes(self, monkeypatch):
        conn = FakeRedis()
        last = datetime(2026, 8, 5, 12, 0, 0)
        conn.store[au.GITHUB_GATE_KEY] = last.isoformat().encode()  # bytes, like prod
        monkeypatch.setattr(au, "_redis", lambda: conn)
        assert au.next_run_github() == int((last + timedelta(minutes=30)).timestamp())

    def test_missing_or_malformed_gate(self, monkeypatch):
        conn = FakeRedis()
        monkeypatch.setattr(au, "_redis", lambda: conn)
        assert au.next_run_github() is None
        conn.store[au.GITHUB_GATE_KEY] = b"not-a-date"
        assert au.next_run_github() is None


class TestEmbedBuilders:
    def test_change_embed_success(self):
        embed = au.build_change_embed("wom_sync", True, ["a", "b"], now=1000)
        assert embed["title"] == "WOM fork sync — changes applied"
        assert embed["description"] == "• a\n• b"
        assert embed["color"] == au.COLOR_OK

    def test_change_embed_failure_includes_error(self):
        embed = au.build_change_embed("github_pages", False, [], error="boom", now=1000)
        assert "FAILED" in embed["title"]
        assert "boom" in embed["description"]
        assert embed["color"] == au.COLOR_FAIL

    def test_change_embed_truncates_description(self):
        embed = au.build_change_embed("wom_sync", True, ["x" * 500] * 20, now=1000)
        assert len(embed["description"]) <= au._DESCRIPTION_LIMIT + 1

    def test_status_embed_lists_both_jobs(self):
        states = {
            "github_pages": {"ts": 900, "ok": True, "changes": 3},
            "wom_sync": {"ts": 800, "ok": True, "changes": 0},
        }
        embed = au.build_status_embed(states, {"github_pages": 2000, "wom_sync": 3000}, now=1000)
        names = [f["name"] for f in embed["fields"]]
        assert names == ["GitHub Pages publisher", "WOM fork sync"]
        gh_field, wom_field = embed["fields"]
        assert "<t:900:R>" in gh_field["value"]
        assert "3 change(s)" in gh_field["value"]
        assert "<t:2000:R>" in gh_field["value"]
        assert "no changes" in wom_field["value"]
        assert embed["color"] == au.COLOR_NEUTRAL

    def test_status_embed_failure_and_unknowns(self):
        states = {"github_pages": {"ts": 900, "ok": False, "error": "kaput\ndetail"}}
        embed = au.build_status_embed(states, {}, now=1000)
        gh_field, wom_field = embed["fields"]
        assert "FAILED: kaput" in gh_field["value"]
        assert "detail" not in gh_field["value"]  # first line only
        assert "never" in wom_field["value"]
        assert "Next run: unknown" in gh_field["value"]
        assert embed["color"] == au.COLOR_FAIL


class TestRefreshStatus:
    def _setup(self, monkeypatch, conn):
        monkeypatch.setattr(au, "_redis", lambda: conn)
        monkeypatch.setattr(au, "next_run_github", lambda: 2000)
        monkeypatch.setattr(au, "next_run_wom", lambda: 3000)

    def test_first_run_posts_and_stores(self, monkeypatch):
        conn, rest = FakeRedis(), FakeRest()
        self._setup(monkeypatch, conn)
        asyncio.run(au._refresh_status(rest, conn, "42", reposted_above=False))
        assert len(rest.posts) == 1
        assert conn.store[au.STATUS_MESSAGE_KEY] == "101"

    def test_quiet_run_edits_in_place(self, monkeypatch):
        conn, rest = FakeRedis(), FakeRest()
        conn.store[au.STATUS_MESSAGE_KEY] = b"555"
        self._setup(monkeypatch, conn)
        asyncio.run(au._refresh_status(rest, conn, "42", reposted_above=False))
        assert rest.edits and rest.edits[0][1] == "555"
        assert not rest.posts and not rest.deletes
        assert conn.store[au.STATUS_MESSAGE_KEY] == b"555"

    def test_change_posted_above_deletes_and_reposts(self, monkeypatch):
        conn, rest = FakeRedis(), FakeRest()
        conn.store[au.STATUS_MESSAGE_KEY] = "555"
        self._setup(monkeypatch, conn)
        asyncio.run(au._refresh_status(rest, conn, "42", reposted_above=True))
        assert rest.deletes == [("42", "555")]
        assert len(rest.posts) == 1
        assert not rest.edits
        assert conn.store[au.STATUS_MESSAGE_KEY] == "101"

    def test_human_deleted_status_reposts(self, monkeypatch):
        conn, rest = FakeRedis(), FakeRest()
        conn.store[au.STATUS_MESSAGE_KEY] = "555"
        rest.edit_raises = NotFound("gone")
        self._setup(monkeypatch, conn)
        asyncio.run(au._refresh_status(rest, conn, "42", reposted_above=False))
        assert len(rest.posts) == 1
        assert conn.store[au.STATUS_MESSAGE_KEY] == "101"

    def test_stale_id_on_delete_is_swallowed(self, monkeypatch):
        conn, rest = FakeRedis(), FakeRest()
        conn.store[au.STATUS_MESSAGE_KEY] = "555"
        rest.delete_raises = NotFound("already gone")
        self._setup(monkeypatch, conn)
        asyncio.run(au._refresh_status(rest, conn, "42", reposted_above=True))
        assert len(rest.posts) == 1

    def test_non_404_edit_error_propagates(self, monkeypatch):
        conn, rest = FakeRedis(), FakeRest()
        conn.store[au.STATUS_MESSAGE_KEY] = "555"
        rest.edit_raises = RuntimeError("discord melted")
        self._setup(monkeypatch, conn)
        try:
            asyncio.run(au._refresh_status(rest, conn, "42", reposted_above=False))
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError to propagate")


class TestReportRun:
    def _setup(self, monkeypatch, conn):
        FakeRest.instances = []
        monkeypatch.setattr(au, "_dev_mode", lambda: False)
        monkeypatch.setattr(au, "_channel_id", lambda: "42")
        monkeypatch.setattr(au, "_token", lambda: "tok")
        monkeypatch.setattr(au, "_redis", lambda: conn)
        monkeypatch.setattr(au, "next_run_github", lambda: 2000)
        monkeypatch.setattr(au, "next_run_wom", lambda: 3000)
        monkeypatch.setitem(
            sys.modules, "utils.discord_rest", SimpleNamespace(DiscordRest=FakeRest)
        )

    def test_changes_post_change_then_repost_status(self, monkeypatch):
        conn = FakeRedis()
        conn.store[au.STATUS_MESSAGE_KEY] = "555"
        self._setup(monkeypatch, conn)
        asyncio.run(au.report_run("wom_sync", ok=True, changes=["bumped fork"]))
        rest = FakeRest.instances[0]
        assert len(rest.posts) == 2  # change message + fresh status
        assert rest.deletes == [("42", "555")]
        state = json.loads(conn.store[au.JOB_STATE_KEY.format(job="wom_sync")])
        assert state["ok"] is True and state["changes"] == 1
        assert au.LOCK_KEY not in conn.store  # lock released

    def test_no_changes_edits_status_only(self, monkeypatch):
        conn = FakeRedis()
        conn.store[au.STATUS_MESSAGE_KEY] = "555"
        self._setup(monkeypatch, conn)
        asyncio.run(au.report_run("github_pages", ok=True, changes=[]))
        rest = FakeRest.instances[0]
        assert not rest.posts and not rest.deletes
        assert rest.edits and rest.edits[0][1] == "555"

    def test_failure_posts_red_message(self, monkeypatch):
        conn = FakeRedis()
        self._setup(monkeypatch, conn)
        asyncio.run(au.report_run("wom_sync", ok=False, changes=[], error="pip failed"))
        rest = FakeRest.instances[0]
        change_payload = rest.posts[0][1]
        assert change_payload["embeds"][0]["color"] == au.COLOR_FAIL
        state = json.loads(conn.store[au.JOB_STATE_KEY.format(job="wom_sync")])
        assert state["ok"] is False and state["error"] == "pip failed"

    def test_foreign_lock_does_not_block_or_get_deleted(self, monkeypatch):
        conn = FakeRedis()
        conn.store[au.LOCK_KEY] = "someone-else"
        self._setup(monkeypatch, conn)

        async def _no_sleep(_secs):
            return None

        monkeypatch.setattr(au.asyncio, "sleep", _no_sleep)
        asyncio.run(au.report_run("wom_sync", ok=True, changes=["x"]))
        rest = FakeRest.instances[0]
        assert rest.posts  # still reported, degraded to lockless
        assert conn.store[au.LOCK_KEY] == "someone-else"

    def test_redis_down_posts_change_only(self, monkeypatch):
        self._setup(monkeypatch, None)
        asyncio.run(au.report_run("wom_sync", ok=True, changes=["x"]))
        rest = FakeRest.instances[0]
        assert len(rest.posts) == 1  # change message, no status upkeep
        assert not rest.edits and not rest.deletes

    def test_dev_mode_is_silent(self, monkeypatch):
        conn = FakeRedis()
        self._setup(monkeypatch, conn)
        monkeypatch.setattr(au, "_dev_mode", lambda: True)
        asyncio.run(au.report_run("wom_sync", ok=True, changes=["x"]))
        assert not FakeRest.instances
        # the run is still recorded even when Discord is skipped
        assert au.JOB_STATE_KEY.format(job="wom_sync") in conn.store

    def test_never_raises(self, monkeypatch):
        conn = FakeRedis()
        self._setup(monkeypatch, conn)

        class ExplodingRest(FakeRest):
            async def post_message(self, channel_id, payload):
                raise RuntimeError("discord is down")

        monkeypatch.setitem(
            sys.modules, "utils.discord_rest", SimpleNamespace(DiscordRest=ExplodingRest)
        )
        asyncio.run(au.report_run("wom_sync", ok=True, changes=["x"]))  # must not raise
        assert au.LOCK_KEY not in conn.store  # lock still released
