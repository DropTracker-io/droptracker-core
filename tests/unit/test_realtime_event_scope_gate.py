"""Private events must not be watchable over SSE (web_api/routes/realtime.py).

The stream treated ``event:{id}`` as public like ``group:*``, but its frames
are display-ready — player names, task labels, points, team scores — so anyone
who knew the id could follow a private event live that its own page 404s for
them. The gate now applies the same rule the HTTP routes do.

Three properties are worth pinning, because each fails silently:
  * a public event stays watchable by anyone, including anonymously (this is
    what every live event page depends on);
  * a private event is refused unless the viewer passes the same
    ``_can_view_restricted`` check the event page uses;
  * anything unexpected — missing event, a lookup that raises — is refused,
    not allowed. A privacy gate that fails open is not a gate.
"""
from __future__ import annotations

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
    def __init__(self, event):
        self._event = event

    def query(self, *a, **k):
        return _Query(self._event)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clear_cache():
    # The module caches the privacy answer for 60s; each test must decide it.
    rt._event_privacy_cache.clear()
    yield
    rt._event_privacy_cache.clear()


def _install(monkeypatch, event, *, can_view=False):
    monkeypatch.setattr(rt, "db_session", lambda: _Session(event), raising=False)
    import web_api.common as common

    monkeypatch.setattr(common, "db_session", lambda: _Session(event))
    import web_api.routes.events as evr

    monkeypatch.setattr(evr, "_can_view_restricted", lambda s, uid, ev: can_view)


def _event(visibility="public", eid=7):
    return SimpleNamespace(id=eid, status="active", ends_at=None, visibility=visibility)


class TestPublicEvents:
    def test_anonymous_may_watch_a_public_event(self, monkeypatch):
        _install(monkeypatch, _event())
        assert rt._may_watch_event(None, 7) is True

    def test_signed_in_may_watch_a_public_event(self, monkeypatch):
        _install(monkeypatch, _event())
        assert rt._may_watch_event(123, 7) is True


class TestPrivateEvents:
    def test_anonymous_is_refused(self, monkeypatch):
        _install(monkeypatch, _event(visibility="private"))
        assert rt._may_watch_event(None, 7) is False

    def test_outsider_is_refused(self, monkeypatch):
        _install(monkeypatch, _event(visibility="private"), can_view=False)
        assert rt._may_watch_event(999, 7) is False

    def test_participant_is_allowed(self, monkeypatch):
        # Same audience rule as the event page: admins + participating members.
        _install(monkeypatch, _event(visibility="private"), can_view=True)
        assert rt._may_watch_event(123, 7) is True


class TestFailsClosed:
    def test_missing_event_is_refused(self, monkeypatch):
        _install(monkeypatch, None)
        assert rt._may_watch_event(123, 7) is False

    def test_a_raising_lookup_is_refused(self, monkeypatch):
        def _boom():
            raise RuntimeError("database on fire")

        import web_api.common as common

        monkeypatch.setattr(common, "db_session", _boom)
        assert rt._may_watch_event(123, 7) is False


class TestPrivacyCache:
    def test_a_public_answer_short_circuits_the_next_call(self, monkeypatch):
        calls = {"n": 0}

        class _CountingSession(_Session):
            def query(self, *a, **k):
                calls["n"] += 1
                return _Query(_event())

        import web_api.common as common

        monkeypatch.setattr(common, "db_session", lambda: _CountingSession(_event()))
        assert rt._may_watch_event(None, 7) is True
        assert rt._may_watch_event(None, 7) is True
        # Second call is served from the cache — the point of caching only the
        # viewer-independent half.
        assert calls["n"] == 1

    def test_a_private_answer_is_never_cached_as_allowed(self, monkeypatch):
        _install(monkeypatch, _event(visibility="private"), can_view=False)
        assert rt._may_watch_event(None, 7) is False
        # Still refused on the second call: only "is it private" is cached, the
        # per-viewer decision is always recomputed.
        assert rt._may_watch_event(None, 7) is False
