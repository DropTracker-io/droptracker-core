"""Regression test: the queue consumer must reproduce the synchronous intake
path's video linkage.

Before this fix, flipping WEBHOOK_QUEUE_MODE=true silently dropped all video
linkage because workers/webhook_consumer._process_entry never called
_link_video_to_submission (pre-dispatch) or _try_attach_video_url_to_drop
(post-drop) the way api.routes.webhook._process_webhook_request does.
"""

import json
from unittest.mock import AsyncMock, MagicMock

from api import core as api_core
import api.routes.webhook as webhook
import data.submissions as submissions
from workers.webhook_consumer import _process_entry


async def test_consumer_links_video_for_drop(monkeypatch):
    processed = {"type": "drop", "world_type": "main", "player": "Zezima", "guid": "g-1"}

    monkeypatch.setattr(webhook, "process_webhook_data", AsyncMock(return_value=[processed]))
    monkeypatch.setattr(api_core, "get_db_session", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(api_core, "reset_db_connections", MagicMock())

    drop_processor = AsyncMock()
    link_video = AsyncMock()
    attach_url = AsyncMock()
    monkeypatch.setattr(submissions, "drop_processor", drop_processor)
    monkeypatch.setattr(webhook, "_link_video_to_submission", link_video)
    monkeypatch.setattr(webhook, "_try_attach_video_url_to_drop", attach_url)

    await _process_entry(json.dumps({"payload": {"type": "drop"}}).encode())

    drop_processor.assert_awaited_once()
    link_video.assert_awaited_once()   # pre-dispatch video linkage
    attach_url.assert_awaited_once()   # post-drop URL attach


async def test_consumer_links_video_for_non_drop(monkeypatch):
    """Non-drop types still get _link_video_to_submission but not the drop-only attach."""
    processed = {"type": "collection_log", "world_type": "main", "player": "Zezima", "guid": "g-2"}

    monkeypatch.setattr(webhook, "process_webhook_data", AsyncMock(return_value=[processed]))
    monkeypatch.setattr(api_core, "get_db_session", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(api_core, "reset_db_connections", MagicMock())

    clog_processor = AsyncMock()
    link_video = AsyncMock()
    attach_url = AsyncMock()
    monkeypatch.setattr(submissions, "clog_processor", clog_processor)
    monkeypatch.setattr(webhook, "_link_video_to_submission", link_video)
    monkeypatch.setattr(webhook, "_try_attach_video_url_to_drop", attach_url)

    await _process_entry(json.dumps({"payload": {"type": "collection_log"}}).encode())

    clog_processor.assert_awaited_once()
    link_video.assert_awaited_once()
    attach_url.assert_not_awaited()
