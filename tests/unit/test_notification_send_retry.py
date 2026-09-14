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
import time
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

    def test_an_abandoned_send_is_transient(self):
        # channel.send returning None (library gave up on 429s) is surfaced as
        # SendRateLimited so the row is requeued rather than marked delivered.
        assert ns.NotificationService._is_transient_send_error(
            ns.SendRateLimited("gave up")
        )

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


class TestSendWrapper:
    """`_send` is the single place that decides a send actually happened."""

    @pytest.fixture()
    def service(self):
        return ns.NotificationService.__new__(ns.NotificationService)

    async def test_a_real_message_is_returned(self, service):
        sentinel = object()

        async def send(*a, **k):
            return sentinel

        assert await service._send(SimpleNamespace(send=send), "hi") is sentinel

    async def test_none_raises_instead_of_reporting_success(self, service):
        async def send(*a, **k):
            return None

        with pytest.raises(ns.SendRateLimited):
            await service._send(SimpleNamespace(send=send), "hi")

    async def test_arguments_are_passed_through_untouched(self, service):
        seen = {}

        async def send(*a, **k):
            seen["args"], seen["kwargs"] = a, k
            return object()

        await service._send(SimpleNamespace(send=send), "content", embed="E", files="F")
        assert seen["args"] == ("content",)
        assert seen["kwargs"] == {"embed": "E", "files": "F"}


def _snowflake_at(ms):
    return (int(ms) - ns._DISCORD_EPOCH_MS) << 22


class _RecordingChannel:
    """A channel whose sends are recorded; ``landed_then_dropped`` makes the
    next send behave like 2026-09-13: Discord creates the message, then the
    connection dies while the reply is read."""

    def __init__(self, channel_id):
        self.id = channel_id
        self.calls = []
        self.landed_then_dropped = False

    async def send(self, *args, **kwargs):
        self.calls.append(kwargs)
        if self.landed_then_dropped:
            self.landed_then_dropped = False
            raise ConnectionError(
                "[SSL: APPLICATION_DATA_AFTER_CLOSE_NOTIFY] application data after close notify"
            )
        return SimpleNamespace(id=_snowflake_at(time.time() * 1000))


class TestSendsInsideAQueueRowCarryANonce:
    """A transient error can arrive after Discord created the message, so the
    requeue is only safe if the retry names the same message. Three drops were
    posted twice in two channels each (2026-09-10, -12, -13) before this."""

    @pytest.fixture()
    def service(self):
        return ns.NotificationService.__new__(ns.NotificationService)

    async def _send_in_row(self, service, notification_id, channel, n=1):
        token = ns._send_scope.set(ns._SendScope(notification_id))
        try:
            for _ in range(n):
                await service._send(channel, "content", embed="E")
        finally:
            ns._send_scope.reset(token)

    async def test_the_send_is_marked_enforced_and_fits_discords_cap(self, service):
        channel = _RecordingChannel(1504942190672216074)
        await self._send_in_row(service, 2128909, channel)
        (kwargs,) = channel.calls
        assert kwargs["enforce_nonce"] is True
        assert isinstance(kwargs["nonce"], str) and 0 < len(kwargs["nonce"]) <= 25
        assert kwargs["embed"] == "E"

    async def test_a_retry_of_the_same_row_reuses_the_nonce(self, service):
        first, retry = _RecordingChannel(10), _RecordingChannel(10)
        await self._send_in_row(service, 2128909, first)
        await self._send_in_row(service, 2128909, retry)
        assert first.calls[0]["nonce"] == retry.calls[0]["nonce"]

    async def test_distinct_messages_get_distinct_nonces(self, service):
        a, b = _RecordingChannel(10), _RecordingChannel(11)
        await self._send_in_row(service, 1, a, n=2)
        await self._send_in_row(service, 1, b)
        other_row = _RecordingChannel(10)
        await self._send_in_row(service, 2, other_row)
        nonces = [c["nonce"] for c in a.calls + b.calls + other_row.calls]
        assert len(set(nonces)) == 4

    async def test_a_retry_reaching_channels_in_another_order_still_matches(self, service):
        # Keyed per channel, so skipping or reordering a channel on the retry
        # cannot hand one channel's message to another.
        a1, b1 = _RecordingChannel(10), _RecordingChannel(11)
        token = ns._send_scope.set(ns._SendScope(7))
        try:
            await service._send(a1, embed="A")
            await service._send(b1, embed="B")
        finally:
            ns._send_scope.reset(token)
        b2 = _RecordingChannel(11)
        await self._send_in_row(service, 7, b2)
        assert b2.calls[0]["nonce"] == b1.calls[0]["nonce"]
        assert b2.calls[0]["nonce"] != a1.calls[0]["nonce"]

    async def test_a_caller_supplied_nonce_is_left_alone(self, service):
        channel = _RecordingChannel(10)
        token = ns._send_scope.set(ns._SendScope(7))
        try:
            await service._send(channel, embed="E", nonce="mine")
        finally:
            ns._send_scope.reset(token)
        assert channel.calls[0] == {"embed": "E", "nonce": "mine"}

    def test_an_earlier_message_is_recognised_as_a_nonce_match(self, service):
        now_ms = time.time() * 1000
        landed_earlier = SimpleNamespace(id=_snowflake_at(now_ms - 10_000))
        just_created = SimpleNamespace(id=_snowflake_at(now_ms + 300))
        assert service._created_before(landed_earlier, now_ms)
        assert not service._created_before(just_created, now_ms)
        assert not service._created_before(SimpleNamespace(id=None), now_ms)


class _FakeRowSession:
    def __init__(self, row):
        self.row = row
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def with_for_update(self, *a, **k):
        return self

    def first(self):
        return self.row if self.row.status in ("pending", "video_processing") else None

    def commit(self):
        self.commits += 1


class TestProcessOneNotificationScopesTheRow:
    @pytest.fixture()
    def service(self, monkeypatch):
        svc = ns.NotificationService.__new__(ns.NotificationService)
        monkeypatch.setattr(ns, "redis_client", SimpleNamespace(client=_FakeRedis()))
        self.row = SimpleNamespace(id=2128909, status="pending", error_message=None)
        session = _FakeRowSession(self.row)
        monkeypatch.setattr(sys.modules["api.core"], "get_db_session", lambda: session, raising=False)
        self.channel = _RecordingChannel(1504942190672216074)

        async def send_path(notification, db_session):
            await svc._send(self.channel, "Big1ronDave received a drop:", embed="Magus vestige")
            notification.status = "sent"

        monkeypatch.setattr(svc, "process_notification_with_session", send_path)
        return svc

    async def test_the_requeued_attempt_repeats_the_first_attempts_nonce(self, service):
        self.channel.landed_then_dropped = True
        assert await service._process_one_notification(self.row.id) == 0
        assert self.row.status == "pending"  # transient: requeued
        assert await service._process_one_notification(self.row.id) == 1
        first, retry = self.channel.calls
        assert first["enforce_nonce"] and retry["enforce_nonce"]
        assert first["nonce"] == retry["nonce"]

    async def test_the_scope_ends_with_the_row(self, service):
        assert await service._process_one_notification(self.row.id) == 1
        assert ns._send_scope.get() is None
        outside = _RecordingChannel(5)
        await service._send(outside, embed="E")
        assert outside.calls == [{"embed": "E"}]
