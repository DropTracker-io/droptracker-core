"""The "Modify Entry" modals acknowledge Discord before doing the work.

Editing a value or the split players re-awards points, refreshes the
leaderboards and rebuilds the notification before replying. That routinely
outlasts Discord's 3-second window, and the modal then showed "Something went
wrong" even though the change was saved (Frontier PvM, 2026-11-02). The handler
defers first; these tests pin that order.

``services/entry_modifier.py`` is loaded from its file path, because the
conftest stubs the ``services`` package (same as test_points_ephemeral_replies).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _name in ("interactions.api", "interactions.api.events"):
    sys.modules.setdefault(_name, MagicMock())

_spec = importlib.util.spec_from_file_location(
    "_entry_modifier_defer_ut", os.path.join(_ROOT, "services", "entry_modifier.py"),
)
em = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(em)


def _run(custom_id):
    calls = []
    ctx = SimpleNamespace(custom_id=custom_id)
    ctx.defer = AsyncMock(side_effect=lambda **kw: calls.append(("defer", kw)))
    ext = SimpleNamespace(
        _process_edit_value=AsyncMock(side_effect=lambda c: calls.append(("edit_value", None))),
        _process_modify_splits=AsyncMock(side_effect=lambda c: calls.append(("splits", None))),
    )
    asyncio.run(em._dispatch_entry_modal(ext, ctx))
    return calls


@pytest.mark.parametrize("custom_id, work", [
    ("entry_modify_splits_modal_123", "splits"),
    ("entry_edit_value_modal_123", "edit_value"),
])
def test_defers_privately_before_the_work(custom_id, work):
    assert _run(custom_id) == [("defer", {"ephemeral": True}), (work, None)]


def test_other_modals_are_left_alone():
    # Every ModalCompletion in the bot reaches this listener; deferring someone
    # else's modal would answer it on their behalf.
    assert _run("ticket_create_modal_9") == []
