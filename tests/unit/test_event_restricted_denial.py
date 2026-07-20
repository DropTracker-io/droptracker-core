"""Restricted-event denial semantics (web57a).

Signed-in viewers denied on a draft/private event get a 403 carrying a
machine-readable ``code`` extension member so the site can explain WHY;
anonymous viewers fall through (helper returns) to the caller's anonymized
404, keeping restricted events indistinguishable from missing ones when
logged out.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import web_api.routes.events as evr
from web_api import deps
from web_api.common import ProblemException


def _event(status: str = "active", visibility: str = "public"):
    return SimpleNamespace(id=42, status=status, ends_at=None, visibility=visibility)


class TestDenyRestricted:
    def test_anonymous_returns_so_caller_404s(self):
        # No exception: the route's own "Event not found" 404 must fire.
        assert evr._deny_restricted(_event(status="draft"), None) is None

    def test_signed_in_draft_gets_403_with_code(self):
        with pytest.raises(ProblemException) as exc:
            evr._deny_restricted(_event(status="draft"), 7)
        assert exc.value.status == 403
        assert exc.value.extra == {"code": "event_draft"}

    def test_signed_in_private_gets_403_with_code(self):
        with pytest.raises(ProblemException) as exc:
            evr._deny_restricted(_event(status="active", visibility="private"), 7)
        assert exc.value.status == 403
        assert exc.value.extra == {"code": "event_private"}

    def test_draft_wins_over_private(self):
        # A private draft reads as "not published yet" — the actionable reason.
        with pytest.raises(ProblemException) as exc:
            evr._deny_restricted(_event(status="draft", visibility="private"), 7)
        assert exc.value.extra == {"code": "event_draft"}


class TestDepsReasonCodes:
    def test_superadmin_denial_carries_code(self):
        with pytest.raises(ProblemException) as exc:
            deps.assert_superadmin(SimpleNamespace(is_superadmin=False, is_moderator=False))
        assert exc.value.status == 403
        assert exc.value.extra == {"code": "staff_required"}

    def test_moderator_denial_carries_code(self):
        with pytest.raises(ProblemException) as exc:
            deps.assert_moderator(SimpleNamespace(is_superadmin=False, is_moderator=False))
        assert exc.value.status == 403
        assert exc.value.extra == {"code": "moderator_required"}


# ── full request path: GET /events/<id> through the app ─────────────────────
# The gate raises from inside a `_load()` running in asyncio.to_thread; these
# verify the ProblemException propagates out of the thread and the app-level
# handler renders the problem+json body the frontend branches on.

class _StubSession:
    """Context-manager session whose every query chain resolves to one event."""

    def __init__(self, ev):
        self._ev = ev

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._ev


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire_detail(monkeypatch, ev, viewer_id):
    monkeypatch.setattr(evr, "optional_user_id", lambda: viewer_id)
    monkeypatch.setattr(evr, "render_token_authorized", lambda: False)
    monkeypatch.setattr(evr, "db_session", lambda: _StubSession(ev))
    monkeypatch.setattr(evr, "_can_view_restricted", lambda *a, **k: False)


class TestRestrictedEventRequests:
    async def test_signed_in_outsider_gets_reasoned_403(self, client, monkeypatch):
        _wire_detail(monkeypatch, _event(status="draft"), viewer_id=7)
        r = await client.get("/api/v1/events/42")
        assert r.status_code == 403
        body = await r.get_json()
        assert body["title"] == "Event restricted"
        assert body["code"] == "event_draft"

    async def test_anonymous_gets_anonymized_404(self, client, monkeypatch):
        _wire_detail(monkeypatch, _event(status="draft"), viewer_id=None)
        r = await client.get("/api/v1/events/42")
        assert r.status_code == 404
        body = await r.get_json()
        # Byte-identical to a genuinely missing event: no code, standard title.
        assert body["title"] == "Event not found"
        assert "code" not in body

    async def test_missing_event_matches_anonymous_denial(self, client, monkeypatch):
        _wire_detail(monkeypatch, None, viewer_id=None)
        r = await client.get("/api/v1/events/42")
        assert r.status_code == 404
        assert (await r.get_json())["title"] == "Event not found"
