"""The components-or-embed branch every notification send path now shares.

Two things matter here and neither is about rendering (that is covered in
test_component_layout.py):

1. A layout that does not resolve must leave the embed path untouched. A group
   experimenting with the builder should never lose notifications over it.
2. A components send must close the queue row out exactly like an embed send.
   It did not: the first version set ``status = 'sent'`` on the ORM object and
   returned without committing, so the row stayed 'processing' with a NULL
   processed_at — which is precisely what ``cleanup_stuck_notifications`` resets
   to 'pending', and the notification was then sent again on every sweep.

Loaded directly from the file path (like test_notification_channel_guard.py)
because conftest stubs the ``services`` package.
"""

import importlib
import importlib.util
import os
import re
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# The real module, not ``services.component_layout`` as an attribute of the
# stubbed ``services`` package (which would be another MagicMock).
cl = importlib.import_module("services.component_layout")

for _name in ("services.contribution_notifications", "services.event_notifications"):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MODULE_PATH = os.path.join(_REPO, "services", "notification_service.py")
_spec = importlib.util.spec_from_file_location("_notification_component_send_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_notification_component_send_under_test"] = ns
_spec.loader.exec_module(ns)

NotificationService = ns.NotificationService

LAYOUT = {"accent_color": "#c8aa6e", "blocks": [{"type": "text", "content": "hi {player_name}"}]}
VALUES = {"{player_name}": "Ra ine"}


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _service():
    return NotificationService(MagicMock(), MagicMock())


def _channel():
    return SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1)))


def _notification():
    return SimpleNamespace(id=5, status="processing", processed_at=None, error_message=None)


@pytest.mark.asyncio
async def test_no_active_layout_leaves_the_embed_path_alone(monkeypatch):
    monkeypatch.setattr(cl, "load_active_layout", lambda *a, **k: None)
    channel = _channel()
    sent = await _service()._try_send_component_layout(
        FakeSession(), _notification(), channel, 2, "pb", VALUES)
    assert sent is None
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_sends_components_with_no_content_or_embed(monkeypatch):
    monkeypatch.setattr(cl, "load_active_layout", lambda *a, **k: LAYOUT)
    monkeypatch.setattr(cl, "to_interactions_components", lambda payload: ["<container>"])
    channel = _channel()
    sent = await _service()._try_send_component_layout(
        FakeSession(), _notification(), channel, 2, "pb", VALUES)
    assert sent is not None
    # A V2 message may carry no content and no embeds; Discord rejects the
    # whole message otherwise.
    channel.send.assert_awaited_once_with(components=["<container>"])


@pytest.mark.asyncio
async def test_layout_that_renders_nothing_falls_back(monkeypatch):
    """An image-only layout for a player who sent no screenshot."""
    monkeypatch.setattr(
        cl, "load_active_layout",
        lambda *a, **k: {"blocks": [{"type": "media", "urls": ["{image_url}"]}]})
    channel = _channel()
    sent = await _service()._try_send_component_layout(
        FakeSession(), _notification(), channel, 2, "pb", {"{image_url}": ""})
    assert sent is None
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbuildable_components_fall_back(monkeypatch):
    monkeypatch.setattr(cl, "load_active_layout", lambda *a, **k: LAYOUT)
    monkeypatch.setattr(cl, "to_interactions_components", lambda payload: None)
    channel = _channel()
    sent = await _service()._try_send_component_layout(
        FakeSession(), _notification(), channel, 2, "pb", VALUES)
    assert sent is None
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_raising_layout_never_costs_the_notification(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("bad row")

    monkeypatch.setattr(cl, "load_active_layout", _boom)
    sent = await _service()._try_send_component_layout(
        FakeSession(), _notification(), _channel(), 2, "pb", VALUES)
    assert sent is None


@pytest.mark.asyncio
async def test_send_failures_still_propagate(monkeypatch):
    """A rate limit is a send failure, not a layout failure: it must reach the
    queue's retry handling rather than quietly re-sending as an embed."""
    monkeypatch.setattr(cl, "load_active_layout", lambda *a, **k: LAYOUT)
    monkeypatch.setattr(cl, "to_interactions_components", lambda payload: ["<container>"])
    channel = SimpleNamespace(send=AsyncMock(return_value=None))  # library gave up on 429s
    with pytest.raises(ns.SendRateLimited):
        await _service()._try_send_component_layout(
            FakeSession(), _notification(), channel, 2, "pb", VALUES)


@pytest.mark.asyncio
async def test_finishing_commits_the_row(monkeypatch):
    """The regression guard: without the commit the row is swept back to
    'pending' and the notification is sent all over again."""
    service = _service()
    monkeypatch.setattr(
        service, "_cleanup_processed_local_video_after_send", AsyncMock())
    session = FakeSession()
    notification = _notification()

    await service._finish_component_send(session, notification, {})

    assert notification.status == "sent"
    assert isinstance(notification.processed_at, datetime)
    assert session.commits == 1


def test_every_customisable_type_has_a_send_branch():
    """A type the editor offers but no send path checks would be a layout the
    group can activate and then watch do nothing."""
    src = open(_MODULE_PATH, encoding="utf-8").read()
    wired = set(re.findall(r'_try_send_component_layout\([^)]*?"([a-z_]+)"', src, re.DOTALL))
    assert wired == set(cl.NOTIFICATION_TYPES)


def test_every_branch_finishes_the_send():
    """Each call site must pair the send with _finish_component_send; that
    pairing is the commit, and skipping it re-sends the notification."""
    src = open(_MODULE_PATH, encoding="utf-8").read()
    calls = src.count("await self._try_send_component_layout(")
    finishes = src.count("await self._finish_component_send(")
    assert calls == finishes == len(cl.NOTIFICATION_TYPES)


class Rejected(Exception):
    """Stands in for interactions' BadRequest — conftest stubs the
    ``interactions`` package, so the real class cannot be imported here."""

    status = 400


class Denied(Exception):
    """Stands in for Forbidden (403)."""

    status = 403


@pytest.mark.asyncio
async def test_a_payload_discord_refuses_falls_back_to_the_embed(monkeypatch):
    """A 400 means nothing was posted, so this is still a broken layout — the
    member should get the embed rather than nothing at all."""
    monkeypatch.setattr(cl, "load_active_layout", lambda *a, **k: LAYOUT)
    monkeypatch.setattr(cl, "to_interactions_components", lambda payload: ["<container>"])
    channel = SimpleNamespace(send=AsyncMock(side_effect=Rejected("bad media url")))
    sent = await _service()._try_send_component_layout(
        FakeSession(), _notification(), channel, 2, "pb", VALUES)
    assert sent is None


@pytest.mark.asyncio
async def test_forbidden_still_propagates(monkeypatch):
    """Missing channel permissions must reach the handler that DMs the group's
    admins, not be swallowed into a fallback that fails the same way."""
    monkeypatch.setattr(cl, "load_active_layout", lambda *a, **k: LAYOUT)
    monkeypatch.setattr(cl, "to_interactions_components", lambda payload: ["<container>"])
    channel = SimpleNamespace(send=AsyncMock(side_effect=Denied("no perms")))
    with pytest.raises(Denied):
        await _service()._try_send_component_layout(
            FakeSession(), _notification(), channel, 2, "pb", VALUES)
