"""Chat HTTP surface (web96a) — ``web_api/routes/chat.py``.

Thread ids are small integers, so the endpoints are the last line between a
clan's private negotiation and anyone who can count. The properties worth
pinning:

* **A non-participant gets 404, not 403.** 403 confirms the thread exists,
  which turns id-probing into a map of which clans are talking to which.
* **Posting is gated on the thread's OWN membership**, not on whoever loaded
  the page, and is refused outright once the thread stops being open.
* **You cannot speak for a clan you don't administer**, even when you are a
  legitimate participant via the other side.
* **Deletion is staff-only**, because it is the only takedown path in v1 —
  authors cannot edit or remove their own messages.
* **The rate limiter fails open.** A Redis outage must not silence an
  in-flight negotiation; it is anti-spam, not authorization.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.chat as chat
import web_api.routes.chat as cr

from tests.unit.test_event_auth_modes import _S, _SessionCM


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _thread(**kw):
    base = dict(
        id=7,
        kind="event_invite",
        subject_type="event_group",
        subject_id=55,
        title="Clan A vs Clan B",
        status="open",
        created_at=None,
        last_message_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _member(*parties, can_post=True, moderator=False):
    return chat.Membership(
        parties=tuple(parties or (chat.Party("group", 42),)),
        can_post=can_post,
        is_moderator=moderator,
    )


def _wire(monkeypatch, session, *, user_id=7, membership=None):
    monkeypatch.setattr(cr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(cr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(chat, "resolve_membership", lambda s, t, uid: membership)
    # Redis is not available in unit tests; keep the limiter out of the way
    # unless a test is specifically about it.
    monkeypatch.setattr(cr, "_post_count_this_window", lambda uid: None)


# --------------------------------------------------------------------------- #
# Read access
# --------------------------------------------------------------------------- #
class TestThreadAccess:
    async def test_non_member_gets_404_not_403(self, client, monkeypatch):
        """403 would confirm the thread exists. It must be indistinguishable
        from a thread that was never there."""
        _wire(monkeypatch, _S([_thread()]), membership=None)
        r = await client.get("/api/v1/chat/threads/7")
        assert r.status_code == 404

    async def test_missing_thread_gets_404(self, client, monkeypatch):
        _wire(monkeypatch, _S([]), membership=_member())
        r = await client.get("/api/v1/chat/threads/7")
        assert r.status_code == 404

    async def test_member_reads_the_thread(self, client, monkeypatch):
        s = _S(
            [_thread()],                                    # the thread
            [SimpleNamespace(id=1, party_type="group",      # participants
                             party_id=42, role="member")],
            [(42, "Clan B")],                               # group names
            [(0,)],                                         # last_read lookup
        )
        _wire(monkeypatch, s, membership=_member())
        # unread_counts builds SQL comparisons against ChatMessage, which the
        # conftest db stub makes a MagicMock; its arithmetic is covered in
        # test_chat_service.py.
        monkeypatch.setattr(chat, "unread_counts", lambda s_, ids, uid: {})
        r = await client.get("/api/v1/chat/threads/7")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["id"] == 7
        assert body["can_post"] is True
        assert body["participants"][0]["name"] == "Clan B"


class TestMessageListing:
    async def test_non_member_cannot_read_messages(self, client, monkeypatch):
        _wire(monkeypatch, _S([_thread()]), membership=None)
        r = await client.get("/api/v1/chat/threads/7/messages")
        assert r.status_code == 404

    async def test_member_reads_messages(self, client, monkeypatch):
        msg = SimpleNamespace(
            id=3, thread_id=7, kind="message", author_user_id=11,
            author_party_type="group", author_party_id=42, body="hi",
            attachments_json=None, system_code=None, system_data_json=None,
            created_at=None, deleted_at=None,
        )
        s = _S([_thread()], [msg], [(11, "Zezima")])
        _wire(monkeypatch, s, membership=_member())
        r = await client.get("/api/v1/chat/threads/7/messages")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["messages"][0]["body"] == "hi"
        assert body["messages"][0]["author_name"] == "Zezima"


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #
class TestPosting:
    async def test_non_member_cannot_post(self, client, monkeypatch):
        _wire(monkeypatch, _S([_thread()]), membership=None)
        r = await client.post(
            "/api/v1/chat/threads/7/messages", json={"body": "hello"}
        )
        assert r.status_code == 404

    async def test_locked_thread_refuses_posts(self, client, monkeypatch):
        _wire(
            monkeypatch,
            _S([_thread(status="locked")]),
            membership=_member(can_post=False),
        )
        r = await client.post(
            "/api/v1/chat/threads/7/messages", json={"body": "hello"}
        )
        assert r.status_code == 409

    async def test_empty_message_is_rejected(self, client, monkeypatch):
        _wire(monkeypatch, _S([_thread()]), membership=_member())
        r = await client.post(
            "/api/v1/chat/threads/7/messages", json={"body": "   "}
        )
        assert r.status_code == 422

    async def test_oversized_message_is_rejected(self, client, monkeypatch):
        _wire(monkeypatch, _S([_thread()]), membership=_member())
        r = await client.post(
            "/api/v1/chat/threads/7/messages",
            json={"body": "x" * (chat.BODY_MAX_CHARS + 1)},
        )
        assert r.status_code == 422

    async def test_foreign_attachment_key_is_rejected(self, client, monkeypatch):
        """Only keys our own upload endpoint issued may be rendered."""
        _wire(monkeypatch, _S([_thread()]), membership=_member())
        r = await client.post(
            "/api/v1/chat/threads/7/messages",
            json={"body": "look", "attachments": [{"key": "https://evil/x.png"}]},
        )
        assert r.status_code == 422

    async def test_cannot_post_as_a_clan_you_do_not_administer(
        self, client, monkeypatch
    ):
        """A legitimate participant on ONE side must not be able to put words
        in the other clan's mouth."""
        _wire(
            monkeypatch,
            _S([_thread()]),
            membership=_member(chat.Party("group", 42)),
        )
        r = await client.post(
            "/api/v1/chat/threads/7/messages",
            json={
                "body": "we forfeit",
                "as_party": {"party_type": "group", "party_id": 99},
            },
        )
        assert r.status_code == 403


class TestRateLimit:
    async def test_over_budget_is_429(self, client, monkeypatch):
        _wire(monkeypatch, _S([_thread()]), membership=_member())
        monkeypatch.setattr(
            cr, "_post_count_this_window",
            lambda uid: cr._RATE_LIMIT_MESSAGES + 1,
        )
        r = await client.post(
            "/api/v1/chat/threads/7/messages", json={"body": "spam"}
        )
        assert r.status_code == 429

    async def test_at_the_limit_still_passes(self, client, monkeypatch):
        _wire(monkeypatch, _S([_thread()]), membership=_member())
        monkeypatch.setattr(
            cr, "_post_count_this_window", lambda uid: cr._RATE_LIMIT_MESSAGES
        )
        # Not 429 — the budget is inclusive. (The post itself then fails on the
        # scripted session, which is fine: we only care that the gate passed.)
        r = await client.post(
            "/api/v1/chat/threads/7/messages", json={"body": "ok"}
        )
        assert r.status_code != 429

    def test_unavailable_redis_fails_open(self, monkeypatch):
        """Anti-spam, not authorization: a Redis outage must not silence a
        negotiation in progress."""
        monkeypatch.setattr(cr, "_post_count_this_window", lambda uid: None)
        cr._check_rate_limit(7)  # must not raise

    def test_counter_errors_are_swallowed(self, monkeypatch):
        import web_api.common as common

        def _boom():
            raise RuntimeError("redis is gone")

        monkeypatch.setattr(common, "_rc", _boom)
        assert cr._post_count_this_window(7) is None


# --------------------------------------------------------------------------- #
# Read pointer
# --------------------------------------------------------------------------- #
class TestMarkRead:
    async def test_non_member_cannot_mark_read(self, client, monkeypatch):
        _wire(monkeypatch, _S([_thread()]), membership=None)
        r = await client.post(
            "/api/v1/chat/threads/7/read", json={"message_id": 5}
        )
        assert r.status_code == 404

    async def test_non_integer_message_id_is_422(self, client, monkeypatch):
        _wire(monkeypatch, _S([_thread()]), membership=_member())
        r = await client.post(
            "/api/v1/chat/threads/7/read", json={"message_id": "latest"}
        )
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Moderator takedown
# --------------------------------------------------------------------------- #
class TestDelete:
    async def test_non_staff_cannot_delete(self, client, monkeypatch):
        monkeypatch.setattr(cr, "current_user_id", lambda: 7)
        monkeypatch.setattr(cr, "db_session", lambda: _SessionCM(_S([None])))
        monkeypatch.setattr(cr, "load_user", lambda s, uid: "USER")
        monkeypatch.setattr(cr, "is_superadmin", lambda user: False)
        r = await client.delete("/api/v1/chat/messages/3")
        assert r.status_code == 403

    async def test_staff_tombstones_rather_than_purging(self, client, monkeypatch):
        row = SimpleNamespace(id=3, deleted_at=None, deleted_by_user_id=None)
        s = _S([row])
        monkeypatch.setattr(cr, "current_user_id", lambda: 7)
        monkeypatch.setattr(cr, "db_session", lambda: _SessionCM(s))
        monkeypatch.setattr(cr, "load_user", lambda s_, uid: "USER")
        monkeypatch.setattr(cr, "is_superadmin", lambda user: True)

        r = await client.delete("/api/v1/chat/messages/3")
        assert r.status_code == 200
        # The row survives with a tombstone, and the action is audited.
        assert row.deleted_at is not None
        assert row.deleted_by_user_id == 7
        assert len(s.added) == 1  # the audit row, and nothing removed

    async def test_missing_message_is_404(self, client, monkeypatch):
        monkeypatch.setattr(cr, "current_user_id", lambda: 7)
        monkeypatch.setattr(cr, "db_session", lambda: _SessionCM(_S([])))
        monkeypatch.setattr(cr, "load_user", lambda s, uid: "USER")
        monkeypatch.setattr(cr, "is_superadmin", lambda user: True)
        r = await client.delete("/api/v1/chat/messages/3")
        assert r.status_code == 404
