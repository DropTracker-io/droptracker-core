"""NotificationService._fetch_sendable_channel (services/notification_service.py).

Guards against the 'BaseChannel' object has no attribute 'send' failure mode:
interactions.py resolves channels the bot can see but that cannot receive
messages (categories, forums, unknown types returned as a bare BaseChannel)
to classes without a .send coroutine. The service must mark those
notifications failed with a distinct error instead of crashing.

Loaded directly from the file path (like test_contribution_notifications.py)
because conftest stubs the ``services`` package. The handful of service
modules notification_service imports at module scope that conftest does not
already stub are stubbed here first.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# notification_service imports these sibling service modules at module level;
# the `services` parent package is a MagicMock in conftest, so submodules that
# aren't in sys.modules can't be found by the import machinery.
for _name in (
    "services.contribution_notifications",
    "services.event_notifications",
):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "notification_service.py",
)
_spec = importlib.util.spec_from_file_location("_notification_service_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_notification_service_under_test"] = ns
_spec.loader.exec_module(ns)

NotificationService = ns.NotificationService


def _service_with_channel(channel):
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)
    return NotificationService(bot, MagicMock())


class TestFetchSendableChannel:
    async def test_text_channel_is_returned(self):
        channel = SimpleNamespace(send=AsyncMock())
        service = _service_with_channel(channel)

        resolved, error = await service._fetch_sendable_channel(123)

        assert resolved is channel
        assert error is None

    async def test_missing_channel_reports_not_found(self):
        service = _service_with_channel(None)

        resolved, error = await service._fetch_sendable_channel(456)

        assert resolved is None
        assert error == "Channel 456 not found"

    async def test_channel_without_send_is_rejected(self):
        # Categories/forums (and bare BaseChannel for unknown types) have no
        # .send coroutine in interactions.py.
        category_like = SimpleNamespace(id=789, name="a category")
        service = _service_with_channel(category_like)

        resolved, error = await service._fetch_sendable_channel(789)

        assert resolved is None
        assert error == "Configured channel 789 is not a text channel"

    async def test_non_callable_send_attribute_is_rejected(self):
        weird = SimpleNamespace(send="not a method")
        service = _service_with_channel(weird)

        resolved, error = await service._fetch_sendable_channel(101)

        assert resolved is None
        assert error == "Configured channel 101 is not a text channel"

    async def test_guard_does_not_raise_for_any_channel_shape(self):
        for channel in (None, object(), SimpleNamespace(), 42):
            service = _service_with_channel(channel)
            resolved, error = await service._fetch_sendable_channel(1)
            assert resolved is None
            assert isinstance(error, str) and error
