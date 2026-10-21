"""Unit tests for SuggestionSync._ensure_numbered (thread id numbering).

services/suggestion_sync.py imports the real ``interactions`` package at module
level, which conftest stubs with a bare MagicMock (no submodules). Load the
module under fake ``interactions.*`` submodules, then restore sys.modules.
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "suggestion_sync.py",
)


class _Forbidden(Exception):
    pass


def _fake_interactions():
    root = types.ModuleType("interactions")
    root.Extension = type("Extension", (), {})
    root.listen = lambda *a, **k: (lambda fn: fn)
    events = types.ModuleType("interactions.api.events")
    for n in ("MessageCreate", "MessageDelete", "MessageUpdate", "Startup", "ThreadDelete", "ThreadUpdate"):
        setattr(events, n, type(n, (), {}))
    errors = types.ModuleType("interactions.client.errors")
    errors.Forbidden = _Forbidden
    return {
        "interactions": root,
        "interactions.api": types.ModuleType("interactions.api"),
        "interactions.api.events": events,
        "interactions.client": types.ModuleType("interactions.client"),
        "interactions.client.errors": errors,
    }


with patch.dict(sys.modules, _fake_interactions()):
    _spec = importlib.util.spec_from_file_location("_suggestion_sync_under_test", _MODULE_PATH)
    _sync = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_sync)

BULB = "\N{ELECTRIC LIGHT BULB}"


def _ext():
    ext = object.__new__(_sync.SuggestionSync)  # skip __init__ (needs a running loop)
    ext._numbered = set()
    return ext


def _thread(name="Dark theme", archived=False, tid=555):
    t = MagicMock()
    t.id = tid
    t.name = name
    t.archived = archived
    t.edit = AsyncMock()
    t.send = AsyncMock()
    return t


class TestEnsureNumbered(unittest.TestCase):
    def test_renames_unnumbered_thread(self):
        t = _thread("Dark theme")
        asyncio.run(_ext()._ensure_numbered(t, 12, "suggestion"))
        t.edit.assert_awaited_once()
        self.assertEqual(t.edit.call_args.kwargs["name"], f"{BULB} #12 · Dark theme")
        t.send.assert_not_awaited()

    def test_already_numbered_is_untouched(self):
        t = _thread(f"{BULB} #12 · Dark theme")
        asyncio.run(_ext()._ensure_numbered(t, 12, "suggestion"))
        t.edit.assert_not_awaited()

    def test_archived_thread_is_skipped(self):
        t = _thread(archived=True)
        asyncio.run(_ext()._ensure_numbered(t, 12, "suggestion"))
        t.edit.assert_not_awaited()
        t.send.assert_not_awaited()

    def test_only_one_attempt_per_thread(self):
        ext, t = _ext(), _thread()
        asyncio.run(ext._ensure_numbered(t, 12, "suggestion"))
        asyncio.run(ext._ensure_numbered(t, 12, "suggestion"))
        self.assertEqual(t.edit.await_count, 1)

    def test_forbidden_posts_fallback_note_once(self):
        ext, t = _ext(), _thread()
        t.edit.side_effect = _Forbidden("Missing Permissions")
        asyncio.run(ext._ensure_numbered(t, 12, "bug"))
        asyncio.run(ext._ensure_numbered(t, 12, "bug"))
        t.send.assert_awaited_once_with("-# Tracked as bug report #12")

    def test_forbidden_skips_note_when_history_has_it(self):
        t = _thread()
        t.edit.side_effect = _Forbidden("Missing Permissions")
        prior = MagicMock(content="-# Tracked as bug report #12")
        asyncio.run(_ext()._ensure_numbered(t, 12, "bug", history=[prior]))
        t.send.assert_not_awaited()

    def test_other_errors_are_swallowed(self):
        t = _thread()
        t.edit.side_effect = RuntimeError("boom")
        asyncio.run(_ext()._ensure_numbered(t, 12, "suggestion"))  # must not raise
        t.send.assert_not_awaited()

    def test_note_failure_is_swallowed(self):
        t = _thread()
        t.edit.side_effect = _Forbidden("nope")
        t.send.side_effect = RuntimeError("boom")
        asyncio.run(_ext()._ensure_numbered(t, 12, "bug"))  # must not raise


if __name__ == "__main__":
    unittest.main()
