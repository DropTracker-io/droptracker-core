"""Unit tests for data/submissions/drop.py::_verify_high_value_drop_sync.

This helper runs the >1M item/NPC wiki verification in a worker thread (dispatched
from drop_processor via asyncio.to_thread) so a slow/hanging wiki lookup can't
stall the event loop the systemd-watchdog health check shares. These tests drive
the real helper with a fake osrs_api client to lock in:
  - a definitive wiki result (True/False) is returned unchanged (anti-spoof kept)
  - a lookup that exceeds the timeout fails OPEN (True) — never reject a
    legitimate high-value drop just because the wiki is slow
  - an unexpected error also fails OPEN
"""
import asyncio
import pytest


class _FakeClient:
    def __init__(self, result=None, delay=0.0, exc=None):
        self._result = result
        self._delay = delay
        self._exc = exc
        self.semantic = self  # helper calls client.semantic.check_drop(...)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def check_drop(self, item_name, npc_name):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc:
            raise self._exc
        return self._result


def _patch_client(monkeypatch, **kwargs):
    from data.submissions import common
    monkeypatch.setattr(
        common.osrs_api, "create_client",
        lambda *a, **k: _FakeClient(**kwargs),
    )


def test_definitive_true_passthrough(monkeypatch):
    from data.submissions.drop import _verify_high_value_drop_sync
    _patch_client(monkeypatch, result=True, delay=0.01)
    assert _verify_high_value_drop_sync("Twisted bow", "Chambers of Xeric") is True


def test_definitive_false_passthrough(monkeypatch):
    """A real 'not from NPC' verdict must still reject the drop."""
    from data.submissions.drop import _verify_high_value_drop_sync
    _patch_client(monkeypatch, result=False, delay=0.01)
    assert _verify_high_value_drop_sync("Twisted bow", "Goblin") is False


def test_timeout_fails_open(monkeypatch):
    """A hanging wiki lookup returns True (fail-open) at the timeout boundary."""
    from data.submissions.drop import _verify_high_value_drop_sync
    _patch_client(monkeypatch, result=False, delay=5.0)
    import time
    t0 = time.perf_counter()
    result = _verify_high_value_drop_sync("Scythe of vitur", "Verzik", timeout=0.3)
    elapsed = time.perf_counter() - t0
    assert result is True
    assert elapsed < 1.0, f"should return at ~timeout (0.3s), took {elapsed:.2f}s"


def test_error_fails_open(monkeypatch):
    from data.submissions.drop import _verify_high_value_drop_sync
    _patch_client(monkeypatch, exc=RuntimeError("wiki 503"), delay=0.01)
    assert _verify_high_value_drop_sync("Tumeken's shadow", "Tombs of Amascut") is True
