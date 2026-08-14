"""SSE scope gating for chat (web96a) — ``web_api/routes/realtime.py``.

``rt:chat:{id}`` frames carry message bodies and attachment URLs, so the
subscribe-time check is the only thing between a clan's private negotiation and
anyone who can guess a small integer. Two properties:

* **``chat:`` requires membership**, resolved by the same
  ``services.chat.resolve_membership`` the HTTP routes use, and fails closed on
  a missing thread or a raising lookup.
* **``user:`` compares identities.** The pre-existing ``player:`` branch only
  asks that *some* session exists — copying that here would let any signed-in
  visitor subscribe to anyone's badge feed, so this test pins the difference.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import web_api.routes.realtime as rt


class _Query:
    def __init__(self, result):
        self._result = result

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._result


class _Session:
    def __init__(self, thread):
        self._thread = thread

    def query(self, *a, **k):
        return _Query(self._thread)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install(monkeypatch, thread, *, member=True):
    import web_api.common as common

    monkeypatch.setattr(common, "db_session", lambda: _Session(thread))
    import services.chat as chat

    monkeypatch.setattr(
        chat,
        "resolve_membership",
        lambda s, t, uid: (
            chat.Membership(parties=(chat.Party("group", 1),), can_post=True)
            if member
            else None
        ),
    )


def _thread(tid=7):
    return SimpleNamespace(id=tid, status="open")


# --------------------------------------------------------------------------- #
# _may_read_thread
# --------------------------------------------------------------------------- #
class TestMayReadThread:
    def test_member_is_allowed(self, monkeypatch):
        _install(monkeypatch, _thread(), member=True)
        assert rt._may_read_thread(123, 7) is True

    def test_non_member_is_refused(self, monkeypatch):
        _install(monkeypatch, _thread(), member=False)
        assert rt._may_read_thread(999, 7) is False

    def test_anonymous_is_refused(self, monkeypatch):
        _install(monkeypatch, _thread(), member=True)
        assert rt._may_read_thread(None, 7) is False

    def test_missing_thread_is_refused(self, monkeypatch):
        _install(monkeypatch, None, member=True)
        assert rt._may_read_thread(123, 7) is False

    def test_a_raising_lookup_is_refused(self, monkeypatch):
        """Fails closed: an unreadable thread must never degrade to public."""

        def _boom():
            raise RuntimeError("database on fire")

        import web_api.common as common

        monkeypatch.setattr(common, "db_session", _boom)
        assert rt._may_read_thread(123, 7) is False

    def test_user_zero_is_a_real_user(self, monkeypatch):
        """The site owner is user_id 0 — the guard tests `is None`, so 0 must
        still be able to subscribe."""
        _install(monkeypatch, _thread(), member=True)
        assert rt._may_read_thread(0, 7) is True


# --------------------------------------------------------------------------- #
# _authorize_channels
# --------------------------------------------------------------------------- #
def _authorize(monkeypatch, raw, *, user_id):
    monkeypatch.setattr(rt, "optional_user_id", lambda: user_id)
    return asyncio.run(rt._authorize_channels(raw))


class TestUserScope:
    def test_own_scope_is_allowed(self, monkeypatch):
        assert _authorize(monkeypatch, "user:42", user_id=42) == ["user:42"]

    def test_somebody_elses_scope_is_dropped(self, monkeypatch):
        """The bug this exists to prevent: `player:` only checks that a session
        exists, which would let any signed-in visitor watch another user."""
        assert _authorize(monkeypatch, "user:99", user_id=42) == []

    def test_anonymous_is_dropped(self, monkeypatch):
        assert "user:42" not in _authorize(monkeypatch, "user:42", user_id=None)

    def test_malformed_scope_is_dropped(self, monkeypatch):
        assert _authorize(monkeypatch, "user:abc", user_id=42) == []

    def test_user_zero_may_watch_itself(self, monkeypatch):
        assert _authorize(monkeypatch, "user:0", user_id=0) == ["user:0"]


class TestChatScope:
    def test_member_scope_survives(self, monkeypatch):
        _install(monkeypatch, _thread(), member=True)
        assert _authorize(monkeypatch, "chat:7", user_id=42) == ["chat:7"]

    def test_non_member_scope_is_dropped(self, monkeypatch):
        _install(monkeypatch, _thread(), member=False)
        assert _authorize(monkeypatch, "chat:7", user_id=42) == []

    def test_anonymous_is_dropped(self, monkeypatch):
        _install(monkeypatch, _thread(), member=True)
        assert _authorize(monkeypatch, "chat:7", user_id=None) == []

    def test_malformed_scope_is_dropped(self, monkeypatch):
        _install(monkeypatch, _thread(), member=True)
        assert _authorize(monkeypatch, "chat:not-an-int", user_id=42) == []

    def test_public_scopes_are_unaffected(self, monkeypatch):
        _install(monkeypatch, _thread(), member=False)
        got = _authorize(monkeypatch, "global,feed,chat:7", user_id=42)
        assert got == ["global", "feed"]
