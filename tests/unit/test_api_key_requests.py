"""Asking for a Data API key (web163a): who may ask, and what a decision does.

Run against in-memory SQLite with the real models, because the rules that
matter are about rows: one pending request per subject, a decision happens
once, approval produces exactly one key and one claim link addressed to the
person who asked.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models.api_key_requests import ApiKeyRequest
from db.models.api_key_reveals import ApiKeyReveal
from db.models.api_keys import ApiKey, ApiKeyTier
from db.models.base import Base
from db.models.tickets import Ticket, TicketMessage
from db.models.web import AuditLog

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(_type, _compiler, **_kw):
    # SQLite only autoincrements a column declared exactly INTEGER.
    return "INTEGER"


_ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    """The logic module, by path: under the test bootstrap ``db.<name>`` is
    the *model* module of the same name (see test_api_key_reveals)."""
    spec = importlib.util.spec_from_file_location(f"_real_{name}", _ROOT / "db" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_real_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


akr = _load("api_key_requests")
reveals = _load("api_key_reveals")
keys = _load("api_keys")


@pytest.fixture()
def s(monkeypatch):
    # approve() does ``from db import api_keys, api_key_reveals``.
    monkeypatch.setattr(sys.modules["db"], "api_keys", keys)
    monkeypatch.setattr(sys.modules["db"], "api_key_reveals", reveals)
    monkeypatch.setattr(reveals, "_fernet", lambda key=Fernet(Fernet.generate_key()): key)
    engine = create_engine("sqlite://")
    # Only the tables the flow writes; users/groups are referenced by FK
    # columns SQLite does not enforce.
    tables = [m.__table__ for m in (ApiKeyRequest, ApiKeyReveal, ApiKey, ApiKeyTier,
                                    Ticket, TicketMessage, AuditLog)]
    Base.metadata.create_all(engine, tables=tables)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(uid=42, name="bob"):
    return SimpleNamespace(user_id=uid, username=name, discord_id="123")


GOOD = "A Discord bot that posts our clan's weekly loot totals."


class TestAsking:
    def test_a_personal_request_opens_an_api_token_ticket(self, s):
        req = akr.request_user_key(s, _user(), "  loot   bot ", GOOD)
        s.commit()
        assert (req.scope, req.status, req.label) == ("user", "pending", "loot bot")
        ticket = s.query(Ticket).one()
        assert (ticket.type, ticket.status, ticket.created_by) == ("api_token", "pending", 42)
        first = s.query(TicketMessage).one()
        assert first.origin == "web" and GOOD in first.content
        assert req.ticket_id == ticket.ticket_id

    def test_user_zero_can_ask(self, s):
        # The owner is user 0; nothing here may treat that id as "nobody".
        req = akr.request_user_key(s, _user(uid=0), "x", GOOD)
        s.commit()
        assert req.requested_by_user_id == 0
        with pytest.raises(akr.RequestError) as e:
            akr.request_user_key(s, _user(uid=0), "x", GOOD)
        assert e.value.status == 409

    @pytest.mark.parametrize("purpose", ["", "too short", "x" * 1001])
    def test_the_purpose_is_required(self, s, purpose):
        with pytest.raises(akr.RequestError) as e:
            akr.request_user_key(s, _user(), "x", purpose)
        assert e.value.status == 422
        assert s.query(Ticket).count() == 0

    def test_one_pending_request_per_user(self, s):
        akr.request_user_key(s, _user(), "a", GOOD)
        s.commit()
        with pytest.raises(akr.RequestError) as e:
            akr.request_user_key(s, _user(), "b", GOOD)
        assert e.value.status == 409
        # Somebody else is unaffected.
        akr.request_user_key(s, _user(uid=43), "b", GOOD)

    def test_a_full_key_ring_cannot_ask_for_more(self, s):
        for _ in range(akr.MAX_LIVE_KEYS):
            keys.create_key(s, owner_user_id=42)
        s.commit()
        with pytest.raises(akr.RequestError) as e:
            akr.request_user_key(s, _user(), "x", GOOD)
        assert e.value.status == 409


class TestDeciding:
    def _pending(self, s, uid=42):
        req = akr.request_user_key(s, _user(uid=uid), "loot bot", GOOD)
        s.commit()
        return req

    def test_approval_mints_one_user_key_on_the_entry_tier(self, s):
        req = self._pending(s)
        req, url = akr.approve(s, req.id, 0)
        s.commit()
        key = s.query(ApiKey).one()
        assert (key.scope, key.owner_user_id, key.tier_key) == ("user", 42, "standard")
        assert key.label == "loot bot" and key.created_by_user_id == 0
        assert (req.status, req.api_key_id, req.decided_by_user_id) == ("approved", key.id, 0)
        assert "/api-keys/claim/" in url

    def test_the_claim_link_is_addressed_to_the_requester_only(self, s):
        req = self._pending(s)
        req, url = akr.approve(s, req.id, 0)
        s.commit()
        token = url.rsplit("/", 1)[1]
        assert reveals.claim(s, token, 99)[0] == reveals.FORBIDDEN
        outcome, payload = reveals.claim(s, token, 42)
        assert outcome == reveals.OK and payload["token"].startswith(f"dtk_{req.api_key_id}_")

    def test_the_site_shows_the_link_to_the_requester_until_it_is_spent(self, s):
        req = self._pending(s)
        req, url = akr.approve(s, req.id, 0)
        s.commit()
        assert akr.status_payload(req, s=s, viewer_user_id=42)["claim_url"] == url
        assert akr.status_payload(req, s=s, viewer_user_id=99)["claim_url"] is None
        reveals.claim(s, url.rsplit("/", 1)[1], 42)
        s.commit()
        assert akr.status_payload(req, s=s, viewer_user_id=42)["claim_url"] is None

    def test_an_expired_link_is_not_offered(self, s):
        req = self._pending(s)
        req, _url = akr.approve(s, req.id, 0)
        s.query(ApiKeyReveal).update({"expires_at": datetime.utcnow() - timedelta(hours=1)})
        s.commit()
        assert akr.status_payload(req, s=s, viewer_user_id=42)["claim_url"] is None

    def test_a_request_is_decided_once(self, s):
        req = self._pending(s)
        akr.approve(s, req.id, 0)
        s.commit()
        for again in (akr.approve, akr.deny):
            with pytest.raises(akr.RequestError) as e:
                again(s, req.id, 0)
            assert e.value.status == 409
        assert s.query(ApiKey).count() == 1

    def test_denying_creates_nothing_and_frees_the_slot(self, s):
        req = self._pending(s)
        akr.deny(s, req.id, 7)
        s.commit()
        assert s.query(ApiKey).count() == 0 and req.status == "denied"
        akr.request_user_key(s, _user(), "again", GOOD)

    def test_only_someone_allowed_can_withdraw(self, s):
        req = self._pending(s)
        with pytest.raises(akr.RequestError) as e:
            akr.cancel(s, req.id, 99, may_act=lambda r: False)
        assert e.value.status == 404
        akr.cancel(s, req.id, 42, may_act=lambda r: True)
        assert req.status == "cancelled"
        with pytest.raises(akr.RequestError):
            akr.approve(s, req.id, 0)


class TestWording:
    def test_the_approval_message_carries_the_link_and_the_docs(self):
        text = akr.decision_text({}, "approve", "Staff", "https://x/claim/abc", mention="<@1>")
        assert "https://x/claim/abc" in text and akr.DOCS_URL in text and "<@1>" in text

    def test_a_denial_never_mentions_a_link(self):
        assert "http" not in akr.decision_text({}, "deny", "Staff")

    def test_the_ticket_opening_names_the_group(self):
        text = akr.ticket_opening_text("group", "Iron Lads", "site", GOOD)
        assert "Iron Lads" in text and GOOD in text
