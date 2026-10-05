"""Signed original-receive-time stamps for outage replays (utils/replay_stamp.py).

The R2 drain proves to the intake when the edge first received a submission.
The intake must accept that only with a valid signature, so a client sending
the same header gets nothing out of it.
"""
from datetime import datetime, timezone

import pytest

from utils import replay_stamp


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.delenv("REPLAY_STAMP_KEY", raising=False)
    monkeypatch.setenv("JWT_TOKEN_KEY", "test-secret")


class TestSignVerify:
    def test_round_trip(self):
        stamp = "2026-10-05T12:21:00.123Z"
        sig = replay_stamp.sign(stamp)
        assert replay_stamp.verify(stamp, sig) == "2026-10-05T12:21:00.123000"

    def test_a_forged_signature_is_refused(self):
        assert replay_stamp.verify("2026-10-05T12:21:00Z", "0" * 64) is None

    def test_a_signature_for_another_time_is_refused(self):
        sig = replay_stamp.sign("2026-10-05T12:21:00Z")
        assert replay_stamp.verify("2026-10-01T00:00:00Z", sig) is None

    def test_no_key_means_nothing_is_signed_or_trusted(self, monkeypatch):
        sig = replay_stamp.sign("2026-10-05T12:21:00Z")
        monkeypatch.delenv("JWT_TOKEN_KEY")
        assert replay_stamp.sign("2026-10-05T12:21:00Z") is None
        assert replay_stamp.verify("2026-10-05T12:21:00Z", sig) is None

    def test_offsets_normalize_to_naive_utc(self):
        assert replay_stamp.normalize("2026-10-05T14:00:00+02:00") == "2026-10-05T12:00:00"
        # The same instant written two ways verifies with one signature.
        sig = replay_stamp.sign("2026-10-05T12:00:00Z")
        assert replay_stamp.verify("2026-10-05T14:00:00+02:00", sig) == "2026-10-05T12:00:00"

    def test_garbage_is_neither_signed_nor_verified(self):
        assert replay_stamp.sign("not a date") is None
        assert replay_stamp.verify("not a date", "abc") is None
        assert replay_stamp.verify(None, None) is None


class TestStripClientStampFields:
    def test_drops_every_received_at_key(self):
        data = {"_received_at": "x", "_received_at_trusted": "true",
                "_received_at_anything": 1, "item_name": "Twisted bow"}
        assert replay_stamp.strip_client_stamp_fields(data) == {"item_name": "Twisted bow"}


class TestAcceptorHonoursOnlySignedStamps:
    """api.routes.webhook._replay_received_at: the header alone is worthless."""

    async def _stamp(self, headers):
        from quart import Quart
        from api.routes.webhook import _replay_received_at

        app = Quart(__name__)
        async with app.test_request_context("/webhook", method="POST", headers=headers):
            return _replay_received_at()

    async def test_a_signed_stamp_is_used(self):
        stamp = "2026-10-05T12:21:00Z"
        got = await self._stamp({replay_stamp.STAMP_HEADER: stamp,
                                 replay_stamp.SIG_HEADER: replay_stamp.sign(stamp)})
        assert got == "2026-10-05T12:21:00"

    async def test_an_unsigned_stamp_is_ignored(self):
        assert await self._stamp({replay_stamp.STAMP_HEADER: "2026-10-01T00:00:00Z"}) is None

    async def test_a_badly_signed_stamp_is_ignored(self):
        assert await self._stamp({replay_stamp.STAMP_HEADER: "2026-10-01T00:00:00Z",
                                  replay_stamp.SIG_HEADER: "deadbeef"}) is None

    async def test_no_header_is_the_normal_path(self):
        assert await self._stamp({}) is None
