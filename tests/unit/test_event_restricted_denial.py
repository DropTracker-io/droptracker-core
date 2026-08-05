"""Restricted-event denial semantics (web57a) + the public-draft split (web74a).

Signed-in viewers denied on a private event get a 403 carrying a
machine-readable ``code`` extension member so the site can explain WHY;
anonymous viewers fall through (helper returns) to the caller's anonymized
404, keeping restricted events indistinguishable from missing ones when
logged out.

Drafts no longer restrict: a PUBLIC draft serves its own page to anyone with
the link (so a pre-start Discord link needs no sign-in) and is only kept out
of the public listing — ``_is_restricted`` vs ``_is_unlisted`` below.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import web_api.routes.events as evr
from web_api import deps
from web_api.common import ProblemException


def _event(status: str = "active", visibility: str = "public"):
    return SimpleNamespace(id=42, status=status, ends_at=None, visibility=visibility)


class TestIsRestricted:
    """Content gate: private only. A draft is not a reason to hide a page."""

    def test_public_draft_is_not_restricted(self):
        assert evr._is_restricted(_event(status="draft")) is False

    def test_public_active_is_not_restricted(self):
        assert evr._is_restricted(_event(status="active")) is False

    def test_private_active_is_restricted(self):
        assert evr._is_restricted(_event(status="active", visibility="private")) is True

    def test_private_draft_is_restricted(self):
        assert evr._is_restricted(_event(status="draft", visibility="private")) is True

    def test_missing_visibility_defaults_public(self):
        # Rows written before the column existed read as public, not private.
        assert evr._is_restricted(SimpleNamespace(id=42, status="active", ends_at=None)) is False


class TestIsUnlisted:
    """Listing gate: drafts stay out of the public list even when public."""

    def test_public_draft_is_unlisted(self):
        assert evr._is_unlisted(_event(status="draft")) is True

    def test_private_active_is_unlisted(self):
        assert evr._is_unlisted(_event(status="active", visibility="private")) is True

    def test_public_active_is_listed(self):
        assert evr._is_unlisted(_event(status="active")) is False

    def test_public_past_is_listed(self):
        assert evr._is_unlisted(_event(status="past")) is False


class TestDenyRestricted:
    def test_anonymous_returns_so_caller_404s(self):
        # No exception: the route's own "Event not found" 404 must fire.
        assert evr._deny_restricted(_event(status="active", visibility="private"), None) is None

    def test_signed_in_private_gets_403_with_code(self):
        with pytest.raises(ProblemException) as exc:
            evr._deny_restricted(_event(status="active", visibility="private"), 7)
        assert exc.value.status == 403
        assert exc.value.extra == {"code": "event_private"}

    def test_private_draft_denies_as_private(self):
        # Privacy is the durable reason: it stays denied once the event starts,
        # so "not published yet" would be the wrong thing to tell the viewer.
        with pytest.raises(ProblemException) as exc:
            evr._deny_restricted(_event(status="draft", visibility="private"), 7)
        assert exc.value.extra == {"code": "event_private"}


class TestDepsReasonCodes:
    def test_superadmin_denial_carries_code(self):
        with pytest.raises(ProblemException) as exc:
            deps.assert_superadmin(SimpleNamespace(is_superadmin=False, is_developer=False))
        assert exc.value.status == 403
        assert exc.value.extra == {"code": "staff_required"}

    def test_developer_denial_carries_code(self):
        with pytest.raises(ProblemException) as exc:
            deps.assert_developer(SimpleNamespace(is_superadmin=False, is_developer=False))
        assert exc.value.status == 403
        assert exc.value.extra == {"code": "developer_required"}


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
        _wire_detail(monkeypatch, _event(status="active", visibility="private"), viewer_id=7)
        r = await client.get("/api/v1/events/42")
        assert r.status_code == 403
        body = await r.get_json()
        assert body["title"] == "Event restricted"
        assert body["code"] == "event_private"

    async def test_anonymous_gets_anonymized_404(self, client, monkeypatch):
        _wire_detail(monkeypatch, _event(status="active", visibility="private"), viewer_id=None)
        r = await client.get("/api/v1/events/42")
        assert r.status_code == 404
        body = await r.get_json()
        # Byte-identical to a genuinely missing event: no code, standard title.
        assert body["title"] == "Event not found"
        assert "code" not in body

    async def test_anonymous_reads_a_public_draft(self, client, monkeypatch):
        # web74a: the pre-start Discord link opens without signing in. Note
        # `_can_view_restricted` is still stubbed False — the point is that the
        # gate is never consulted for a public draft.
        _wire_detail(monkeypatch, _event(status="draft"), viewer_id=None)
        monkeypatch.setattr(evr, "_detail", lambda s, ev, viewer_id=None: {"id": ev.id})
        r = await client.get("/api/v1/events/42")
        assert r.status_code == 200
        assert (await r.get_json())["id"] == 42

    async def test_missing_event_matches_anonymous_denial(self, client, monkeypatch):
        _wire_detail(monkeypatch, None, viewer_id=None)
        r = await client.get("/api/v1/events/42")
        assert r.status_code == 404
        assert (await r.get_json())["title"] == "Event not found"
