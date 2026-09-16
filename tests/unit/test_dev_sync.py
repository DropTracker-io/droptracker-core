"""The tester roster push: production's worker and dev's receiving route.

The route must be invisible anywhere but a configured dev instance, and must
refuse anything it cannot open. The worker must push when something changed,
when asked, and periodically regardless — and back off when dev is down.
"""
from __future__ import annotations

import asyncio
import sys
import time

import pytest

from tests.unit import _tester_db as tdb

tr = tdb.load("_tester_roster_for_dev_sync", "services", "tester_roster.py")
route = tdb.load("_dev_sync_route_under_test", "api", "routes", "dev_sync.py")
worker = tdb.load("_dev_sync_worker_under_test", "workers", "dev_sync.py")

KEY = "c2VjcmV0LWtleS1mb3ItdGVzdHMtMDEyMzQ1Njc4OTA="


@pytest.fixture(autouse=True)
def real_roster_module(monkeypatch):
    """`from services import tester_roster` inside the code resolves to the real one."""
    monkeypatch.setattr(sys.modules["services"], "tester_roster", tr, raising=False)


# --------------------------------------------------------------------------- #
# POST /dev-sync/testers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    from quart import Quart

    app = Quart(__name__)
    app.register_blueprint(route.dev_sync_bp)
    return app.test_client()


def _post(client, body):
    async def go():
        response = await client.post("/dev-sync/testers", data=body,
                                     headers={"Content-Type": "text/plain"})
        return response.status_code, await response.get_json()
    return asyncio.run(go())


@pytest.fixture()
def dev_env(monkeypatch):
    monkeypatch.setenv("STATE", "dev")
    monkeypatch.setenv("DEV_SYNC_KEY", KEY)
    return monkeypatch


class TestRoute:
    def test_does_not_exist_in_production(self, client, monkeypatch):
        monkeypatch.setenv("STATE", "live")
        monkeypatch.delenv("STATUS", raising=False)
        monkeypatch.setenv("DEV_SYNC_KEY", KEY)
        status, _ = _post(client, tr.seal(tr.empty_roster(), KEY))
        assert status == 404

    def test_does_not_exist_without_a_key(self, client, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.delenv("DEV_SYNC_KEY", raising=False)
        status, _ = _post(client, tr.seal(tr.empty_roster(), KEY))
        assert status == 404

    def test_an_empty_body_is_rejected(self, client, dev_env):
        status, _ = _post(client, "")
        assert status == 400

    def test_a_token_it_cannot_open_is_refused(self, client, dev_env, monkeypatch):
        called = []
        monkeypatch.setattr(tr, "apply_snapshot", lambda roster: called.append(roster))
        other = "b3RoZXIta2V5LWZvci10ZXN0cy0wMTIzNDU2Nzg5MDE="
        status, _ = _post(client, tr.seal(tr.empty_roster(), other))
        assert status == 403
        assert called == [], "nothing is applied from a token that did not open"

    def test_a_good_token_is_applied(self, client, dev_env, monkeypatch):
        seen = []

        def fake_apply(roster):
            seen.append(roster)
            return 200, {"status": "applied", "testers": 0}

        monkeypatch.setattr(tr, "apply_snapshot", fake_apply)
        status, body = _post(client, tr.seal(tr.empty_roster(), KEY))
        assert status == 200 and body["status"] == "applied"
        assert len(seen) == 1 and seen[0]["users"] == []

    def test_busy_is_passed_through(self, client, dev_env, monkeypatch):
        monkeypatch.setattr(tr, "apply_snapshot", lambda roster: (409, {"status": "busy"}))
        status, body = _post(client, tr.seal(tr.empty_roster(), KEY))
        assert (status, body["status"]) == (409, "busy")

    def test_a_failed_apply_is_a_500_not_a_crash(self, client, dev_env, monkeypatch):
        def boom(roster):
            raise RuntimeError("database is down")

        monkeypatch.setattr(tr, "apply_snapshot", boom)
        status, _ = _post(client, tr.seal(tr.empty_roster(), KEY))
        assert status == 500


# --------------------------------------------------------------------------- #
# workers/dev_sync.py
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Poster:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


APPLIED = Response(200, {"status": "applied", "testers": 1, "skipped": []})
URL = "https://dev-api.example/dev-sync/testers"


def _pusher(*responses):
    clock = Clock()
    poster = Poster(*responses)
    return worker.Pusher(URL, KEY, post=poster, clock=clock), poster, clock


class TestWhenToPush:
    def test_the_first_look_always_pushes(self):
        pusher, _, _ = _pusher()
        assert pusher.due("abc", requested=False)

    def test_an_unchanged_roster_waits(self):
        pusher, _, _ = _pusher(APPLIED)
        pusher.push(tr.empty_roster(), "abc")
        assert not pusher.due("abc", requested=False)

    def test_a_changed_roster_pushes(self):
        pusher, _, _ = _pusher(APPLIED)
        pusher.push(tr.empty_roster(), "abc")
        assert pusher.due("def", requested=False)

    def test_a_request_pushes_even_when_unchanged(self):
        pusher, _, _ = _pusher(APPLIED)
        pusher.push(tr.empty_roster(), "abc")
        assert pusher.due("abc", requested=True)

    def test_an_unchanged_roster_is_resent_eventually(self):
        pusher, _, clock = _pusher(APPLIED)
        pusher.push(tr.empty_roster(), "abc")
        clock.now += worker.RESEND_SECONDS
        assert pusher.due("abc", requested=False), "this is what heals a freshly restored dev"


class TestPush:
    def test_sends_a_sealed_snapshot(self):
        pusher, poster, _ = _pusher(APPLIED)
        roster = dict(tr.empty_roster(), users=[{"user_id": 1, "discord_id": "1", "username": "x"}])
        assert pusher.push(roster, "abc") is True
        sent = poster.calls[0]
        assert sent["url"] == URL
        assert sent["timeout"] == worker.HTTP_TIMEOUT_SECONDS
        opened = tr.unseal(sent["data"].decode("ascii"), KEY)
        assert opened["users"][0]["discord_id"] == "1"

    def test_a_stale_answer_counts_as_delivered(self):
        pusher, _, _ = _pusher(Response(200, {"status": "stale"}))
        assert pusher.push(tr.empty_roster(), "abc") is True
        assert pusher.last_fingerprint == "abc"

    @pytest.mark.parametrize("failure", [
        Response(500, {"error": "boom"}),
        Response(404),
        Response(200, {"status": "something else"}),
        ConnectionError("dev is down"),
    ])
    def test_failures_are_not_remembered_as_delivered(self, failure):
        pusher, _, _ = _pusher(failure)
        assert pusher.push(tr.empty_roster(), "abc") is False
        assert pusher.last_fingerprint is None

    def test_failures_back_off_then_retry(self):
        pusher, _, clock = _pusher(ConnectionError("down"), ConnectionError("down"), APPLIED)
        pusher.push(tr.empty_roster(), "abc")
        assert not pusher.due("abc", requested=True), "backing off, even when asked"
        clock.now += worker.BACKOFF_SECONDS[0]
        assert pusher.due("abc", requested=False)
        pusher.push(tr.empty_roster(), "abc")
        clock.now += worker.BACKOFF_SECONDS[0]
        assert not pusher.due("abc", requested=False), "the second wait is longer"
        clock.now += worker.BACKOFF_SECONDS[1]
        assert pusher.push(tr.empty_roster(), "abc") is True
        assert pusher.failures == 0

    def test_busy_retries_soon(self):
        pusher, _, clock = _pusher(Response(409, {"status": "busy"}))
        assert pusher.push(tr.empty_roster(), "abc") is False
        clock.now += worker.BACKOFF_SECONDS[0]
        assert pusher.due("abc", requested=False)


class TestMain:
    def test_a_dev_instance_never_pushes(self, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        monkeypatch.setenv("DEV_SYNC_URL", URL)
        monkeypatch.setenv("DEV_SYNC_KEY", KEY)
        monkeypatch.setattr(worker, "load_current", lambda: pytest.fail("must not read or push"))
        assert worker.main(["--once"]) == 0

    def test_unconfigured_production_says_so(self, monkeypatch):
        monkeypatch.setenv("STATE", "live")
        monkeypatch.delenv("STATUS", raising=False)
        monkeypatch.delenv("DEV_SYNC_URL", raising=False)
        monkeypatch.delenv("DEV_SYNC_KEY", raising=False)
        monkeypatch.setattr(worker, "load_current", lambda: pytest.fail("must not read or push"))
        assert worker.main(["--once"]) == 1

    def test_once_pushes_one_snapshot(self, monkeypatch):
        monkeypatch.setenv("STATE", "live")
        monkeypatch.delenv("STATUS", raising=False)
        monkeypatch.setenv("DEV_SYNC_URL", URL)
        monkeypatch.setenv("DEV_SYNC_KEY", KEY)
        roster = tr.empty_roster()
        monkeypatch.setattr(worker, "load_current", lambda: (roster, tr.fingerprint(roster)))
        pushed = []
        monkeypatch.setattr(worker.Pusher, "push",
                            lambda self, r, f: pushed.append((self.url, f)) or True)
        assert worker.main(["--once"]) == 0
        assert pushed == [(URL, tr.fingerprint(roster))]
