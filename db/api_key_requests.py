"""Asking for a Data API key, and deciding the request (web163a).

A request is made on the site (a group's API page, or the personal one). It
opens an ``api_token`` support ticket that carries the request card; staff
press Approve or Deny there, or decide from ``/admin/api-keys``. Approval
mints a key on the entry tier and creates a one-time claim link only the
requester can open. Nothing here talks to Discord: the webhook bot posts the
card (``services/api_key_requests_discord.py``) and callers relay the result.

Lives in ``db/`` beside ``api_keys`` and ``api_key_reveals`` so the web API
and the bot share one set of rules. Callers commit.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from db.models.api_key_requests import (
    APPROVED,
    CANCELLED,
    DENIED,
    PENDING,
    SCOPE_GROUP,
    SCOPE_USER,
    ApiKeyRequest,
)

#: Live keys one user or one group may hold; matches the self-serve mint cap.
MAX_LIVE_KEYS = 5
LABEL_MAX = 64
PURPOSE_MIN, PURPOSE_MAX = 20, 1000
#: A claim link from an approved request lives a week: the requester may not
#: see the ticket reply the same day.
CLAIM_TTL_HOURS = 168
DOCS_URL = "https://www.droptracker.io/docs/api"


class RequestError(Exception):
    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status, self.title, self.detail = status, title, detail


def _site_base() -> str:
    return (os.getenv("SITE_URL") or "https://www.droptracker.io").rstrip("/")


def clean_label(raw) -> str:
    return " ".join(str(raw or "").split())[:LABEL_MAX]


def clean_purpose(raw) -> str:
    text = str(raw or "").strip()
    if not (PURPOSE_MIN <= len(text) <= PURPOSE_MAX):
        raise RequestError(
            422, "Tell us more",
            f"Describe what you're building in {PURPOSE_MIN}-{PURPOSE_MAX} characters.")
    return text


def ticket_opening_text(scope: str, subject: str, label: str, purpose: str) -> str:
    """First message of the ticket, written as the requester. Pure."""
    what = (f"a Data API key for the group **{subject}**" if scope == SCOPE_GROUP
            else "a personal Data API key (my own accounts)")
    name = f"\n**Project:** {label}" if label else ""
    return f"I'd like {what}.{name}\n**What it's for:** {purpose}"


def claim_url(reveal_token: str) -> str:
    return f"{_site_base()}/api-keys/claim/{reveal_token}"


def _pending_claim_token(s, req: ApiKeyRequest) -> Optional[str]:
    """The request's claim link, while it can still be opened."""
    if req.status != APPROVED or req.api_key_id is None:
        return None
    from db.models import ApiKeyReveal

    row = (s.query(ApiKeyReveal)
           .filter(ApiKeyReveal.api_key_id == req.api_key_id,
                   ApiKeyReveal.audience_user_id == req.requested_by_user_id,
                   ApiKeyReveal.viewed_at.is_(None),
                   ApiKeyReveal.expires_at > datetime.utcnow())
           .order_by(ApiKeyReveal.id.desc()).first())
    return row.reveal_token if row is not None else None


def status_payload(req: ApiKeyRequest, *, s=None, viewer_user_id: Optional[int] = None,
                   staff: bool = False) -> dict:
    """The request as the site shows it.

    ``claim_url`` is included only for the requester, and only while the link
    is unspent: it is the one thing here that leads to a credential.
    """
    def _ts(dt):
        return int(dt.timestamp()) if dt else None

    out = {
        "id": int(req.id),
        "scope": req.scope,
        "group_id": req.group_id,
        "requested_by_user_id": req.requested_by_user_id,
        "label": req.label or "",
        "purpose": req.purpose or "",
        "status": req.status,
        "ticket_id": req.ticket_id,
        "api_key_id": int(req.api_key_id) if req.api_key_id is not None else None,
        "created_at": _ts(req.created_at),
        "decided_at": _ts(req.decided_at),
        "claim_url": None,
    }
    if (s is not None and viewer_user_id is not None
            and req.requested_by_user_id is not None
            and int(viewer_user_id) == int(req.requested_by_user_id)):
        token = _pending_claim_token(s, req)
        out["claim_url"] = claim_url(token) if token else None
    if staff and s is not None:
        from db.models import Group, User

        user = (s.query(User).filter(User.user_id == req.requested_by_user_id).first()
                if req.requested_by_user_id is not None else None)
        out["requested_by_name"] = getattr(user, "username", None)
        out["decided_by_user_id"] = req.decided_by_user_id
        group = (s.query(Group).filter(Group.group_id == req.group_id).first()
                 if req.group_id is not None else None)
        out["group_name"] = getattr(group, "group_name", None)
    return out


def pending_request(s, scope: str, *, user_id: Optional[int] = None,
                    group_id: Optional[int] = None) -> Optional[ApiKeyRequest]:
    q = s.query(ApiKeyRequest).filter(ApiKeyRequest.scope == scope,
                                      ApiKeyRequest.status == PENDING)
    if scope == SCOPE_GROUP:
        q = q.filter(ApiKeyRequest.group_id == group_id)
    else:
        q = q.filter(ApiKeyRequest.requested_by_user_id == user_id)
    return q.order_by(ApiKeyRequest.id.desc()).first()


def _live_keys(s, *, owner_user_id=None, group_id=None) -> int:
    from db.models import ApiKey

    q = s.query(ApiKey).filter(ApiKey.revoked_at.is_(None))
    if group_id is not None:
        q = q.filter(ApiKey.group_id == group_id)
    else:
        q = q.filter(ApiKey.owner_user_id == owner_user_id)
    return q.count()


def _open_ticket(s, user, subject: str, body: str) -> int:
    from db.models import Ticket, TicketMessage

    ticket = Ticket(
        channel_id=None,
        type="api_token",
        created_by=user.user_id,
        status="pending",
        subject=subject[:255],
        last_reply_uid=str(getattr(user, "discord_id", "") or ""),
    )
    s.add(ticket)
    s.flush()
    s.add(TicketMessage(
        ticket_id=ticket.ticket_id,
        discord_message_id=None,
        author_user_id=user.user_id,
        author_discord_id=str(getattr(user, "discord_id", "") or "0"),
        author_name=(getattr(user, "username", None) or f"user {user.user_id}")[:100],
        is_staff=False,
        is_bot=False,
        kind="message",
        origin="web",
        content=body,
        date_sent=datetime.now(),
    ))
    return ticket.ticket_id


def _audit(s, actor, group_id, action, req, after=None):
    try:
        from db.models import AuditLog

        s.add(AuditLog(actor_user_id=actor, group_id=group_id, action=action,
                       target=f"api_key_requests.{req.id}", before=None, after=after))
    except Exception:
        pass


def _create(s, user, scope: str, group_id, subject: str, label, purpose) -> ApiKeyRequest:
    label = clean_label(label)
    purpose = clean_purpose(purpose)
    req = ApiKeyRequest(scope=scope, group_id=group_id,
                        requested_by_user_id=user.user_id, label=label,
                        purpose=purpose, status=PENDING)
    req.ticket_id = _open_ticket(
        s, user, f"API key request: {subject}",
        ticket_opening_text(scope, subject, label, purpose))
    s.add(req)
    s.flush()
    _audit(s, user.user_id, group_id, "api_key.request", req, scope)
    return req


def request_user_key(s, user, label, purpose) -> ApiKeyRequest:
    if pending_request(s, SCOPE_USER, user_id=user.user_id):
        raise RequestError(409, "Already requested",
                           "You already have a key request waiting for staff.")
    if _live_keys(s, owner_user_id=user.user_id) >= MAX_LIVE_KEYS:
        raise RequestError(409, "Too many keys",
                           f"You already have {MAX_LIVE_KEYS} active keys. Revoke one first.")
    name = getattr(user, "username", None) or f"user {user.user_id}"
    return _create(s, user, SCOPE_USER, None, name, label, purpose)


def request_group_key(s, user, group_id: int, label, purpose) -> ApiKeyRequest:
    """The caller has already been checked as an admin of the group."""
    from db.models import Group

    group = s.query(Group).filter(Group.group_id == group_id).first()
    if group is None:
        raise RequestError(404, "Group not found", "No such group.")
    if group_id == 2:
        raise RequestError(403, "Not allowed",
                           "The global group has no key of its own. Open a support "
                           "ticket if you need to read every group.")
    if pending_request(s, SCOPE_GROUP, group_id=group_id):
        raise RequestError(409, "Already requested",
                           "This group already has a key request waiting for staff.")
    if _live_keys(s, group_id=group_id) >= MAX_LIVE_KEYS:
        raise RequestError(409, "Too many keys",
                           f"This group already has {MAX_LIVE_KEYS} active keys. "
                           "Revoke one first.")
    return _create(s, user, SCOPE_GROUP, group_id, group.group_name, label, purpose)


def _get_pending(s, request_id: int) -> ApiKeyRequest:
    req = (s.query(ApiKeyRequest).filter(ApiKeyRequest.id == request_id)
           .with_for_update().first())
    if req is None:
        raise RequestError(404, "Not found", "No such key request.")
    if req.status != PENDING:
        raise RequestError(409, "Already decided",
                           f"This request was already {req.status}.")
    return req


def approve(s, request_id: int, staff_user_id: Optional[int],
            now: Optional[datetime] = None):
    """Mint the key and its claim link. Returns ``(request, claim_url)``.

    The key starts on the entry tier like every other; staff raise it later
    from the admin page once the traffic has proven itself. The row lock in
    ``_get_pending`` is what stops two staff presses minting two keys.
    """
    from db import api_key_reveals as reveals
    from db import api_keys as keys

    req = _get_pending(s, request_id)
    if req.requested_by_user_id is None:
        raise RequestError(409, "Requester is gone",
                           "The account that asked no longer exists. Deny this request.")
    if req.scope == SCOPE_GROUP:
        row, token = keys.create_key(s, group_id=int(req.group_id), label=req.label,
                                     created_by_user_id=staff_user_id)
    else:
        row, token = keys.create_key(s, owner_user_id=int(req.requested_by_user_id),
                                     label=req.label, created_by_user_id=staff_user_id)
    row.notes = f"Approved from key request #{req.id} (ticket #{req.ticket_id})."
    # Addressed to the person who asked, group key or not: the link is posted
    # in a ticket other people can read, so "any admin of the group" is wider
    # than the conversation staff just had.
    _reveal, reveal_token = reveals.create_reveal(
        s, api_key_id=int(row.id), plaintext=token,
        audience_user_id=int(req.requested_by_user_id),
        created_by_user_id=staff_user_id, ttl_hours=CLAIM_TTL_HOURS)
    req.status = APPROVED
    req.api_key_id = int(row.id)
    req.decided_by_user_id = staff_user_id
    req.decided_at = now or datetime.utcnow()
    _audit(s, staff_user_id, req.group_id, "api_key.request_approve", req,
           f"api_keys.{row.id}")
    return req, claim_url(reveal_token)


def deny(s, request_id: int, staff_user_id: Optional[int],
         now: Optional[datetime] = None) -> ApiKeyRequest:
    req = _get_pending(s, request_id)
    req.status = DENIED
    req.decided_by_user_id = staff_user_id
    req.decided_at = now or datetime.utcnow()
    _audit(s, staff_user_id, req.group_id, "api_key.request_deny", req)
    return req


def cancel(s, request_id: int, actor_user_id: int, *, may_act) -> ApiKeyRequest:
    """Withdraw a pending request. ``may_act(req)`` says whether this caller
    may (the requester, or an admin of the request's group)."""
    req = (s.query(ApiKeyRequest).filter(ApiKeyRequest.id == request_id)
           .with_for_update().first())
    if req is None or not may_act(req):
        raise RequestError(404, "Not found", "No such key request.")
    if req.status != PENDING:
        raise RequestError(409, "Already decided",
                           f"This request was already {req.status}.")
    req.status = CANCELLED
    req.decided_at = datetime.utcnow()
    _audit(s, actor_user_id, req.group_id, "api_key.request_cancel", req)
    return req


def decision_text(req_payload: dict, action: str, by: str, url: Optional[str] = None,
                  mention: str = "") -> str:
    """What the ticket says once staff decide. Pure; shared by bot and site."""
    who = f"{mention} " if mention else ""
    if action == "approve":
        return (
            f"✅ {who}Your API key request was approved by {by}.\n\n"
            f"**Collect your key:** {url}\n"
            "The link opens once, only for the account that asked, while signed in to "
            "the site. It expires in 7 days. The key is shown a single time, so store "
            "it somewhere safe.\n\n"
            f"Reference and example responses: {DOCS_URL}"
        )
    if action == "deny":
        return (f"{who}Your API key request was declined by {by}. No key was created. "
                "Staff will say why here; you can ask again from the site once that's sorted.")
    return "The API key request was withdrawn. No key was created."
