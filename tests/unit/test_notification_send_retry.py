"""Transient-send retry decision (services/notification_service.py).

Audit P1: a Discord 429/5xx or a network blip used to mark the queue row
`failed` terminally — the recipient never got the message and nothing ever
re-attempted it. The service now requeues transient faults a bounded number
of times. Two rules carry the risk:

* the permanent/transient split — retrying a Forbidden hammers a destination
  that will never accept, while failing a 429 terminally drops real messages;
* the bound — without it a hard Discord outage turns the queue into a
  spin loop.

Loaded from the file path like test_notification_channel_guard.py, with the
same sibling-module stubs.
"""

import asyncio
import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest

for _name in (
    "services.contribution_notifications",
    "services.event_notifications",
):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

# conftest stubs `interactions` as a bare module (not a package), so the
# lazy `from interactions.client.errors import ...` inside the code under
# test needs real-shaped exception classes pre-seeded in sys.modules. Other
# test modules (test_event_team_discord_bot) install/reuse the same entry and
# raise these classes with NO arguments, so construction must tolerate both
# styles; upgrade any existing module in place rather than replacing it.
import types  # noqa: E402


class HTTPException(Exception):
    def __init__(self, response=None, text="", **kwargs):
        self.response = response
        self.status = getattr(response, "status", None)
        self.text = text
        super().__init__(text)


class Forbidden(HTTPException):
    pass


class NotFound(HTTPException):
    pass


class BadRequest(HTTPException):
    pass


class RateLimited(HTTPException):
    pass


_errors_mod = sys.modules.get("interactions.client.errors")
if _errors_mod is None:
    _errors_mod = types.ModuleType("interactions.client.errors")
    _client_mod = types.ModuleType("interactions.client")
    _client_mod.errors = _errors_mod
    sys.modules["interactions.client"] = _client_mod
    sys.modules["interactions.client.errors"] = _errors_mod
for _cls in (HTTPException, Forbidden, NotFound, BadRequest, RateLimited):
    setattr(_errors_mod, _cls.__name__, _cls)

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "notification_service.py",
)
_spec = importlib.util.spec_from_file_location("_notification_retry_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_notification_retry_under_test"] = ns
_spec.loader.exec_module(ns)

def _http_error(cls, status, reason="err"):
    return cls(SimpleNamespace(status=status, reason=reason), text="x")


class TestTransientClassification:
    def test_forbidden_is_permanent(self):
        assert not ns.NotificationService._is_transient_send_error(
            _http_error(Forbidden, 403)
        )

    def test_not_found_is_permanent(self):
        assert not ns.NotificationService._is_transient_send_error(
            _http_error(NotFound, 404)
        )

    def test_bad_request_is_permanent(self):
        assert not ns.NotificationService._is_transient_send_error(
            _http_error(BadRequest, 400)
        )

    def test_rate_limited_is_transient(self):
        assert ns.NotificationService._is_transient_send_error(
            _http_error(RateLimited, 429)
        )

    def test_discord_5xx_is_transient(self):
        assert ns.NotificationService._is_transient_send_error(
            _http_error(HTTPException, 502)
        )

    def test_network_errors_are_transient(self):
        assert ns.NotificationService._is_transient_send_error(
            aiohttp.ClientConnectionError()
        )
        assert ns.NotificationService._is_transient_send_error(asyncio.TimeoutError())

    def test_arbitrary_bugs_are_permanent(self):
        assert not ns.NotificationService._is_transient_send_error(
            AttributeError("'str' object has no attribute 'status'")
        )


class _FakeRedis:
    def __init__(self):
        self.counts = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, ttl):
        return True


class TestRetryBound:
    @pytest.fixture()
    def service(self, monkeypatch):
        svc = ns.NotificationService.__new__(ns.NotificationService)
        monkeypatch.setattr(
            ns, "redis_client", SimpleNamespace(client=_FakeRedis())
        )
        return svc

    def test_transient_retries_until_the_cap(self, service):
        exc = _http_error(RateLimited, 429)
        decisions = [service._should_retry_send(7, exc) for _ in range(4)]
        # SEND_ATTEMPTS_MAX = 3 total tries: two requeues, then terminal.
        assert decisions == [True, True, False, False]

    def test_permanent_never_touches_the_counter(self, service):
        assert not service._should_retry_send(7, _http_error(Forbidden, 403))
        assert ns.redis_client.client.counts == {}

    def test_redis_trouble_falls_back_to_terminal(self, service, monkeypatch):
        def _boom(key):
            raise ConnectionError("redis down")

        monkeypatch.setattr(ns.redis_client.client, "incr", _boom)
        assert not service._should_retry_send(7, _http_error(RateLimited, 429))
