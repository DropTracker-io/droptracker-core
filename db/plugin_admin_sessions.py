"""Plugin group-admin device sessions: minting, checking and revoking them.

See ``db/models/plugin_admin_session.py`` for what a session is. The routes
(api/routes/plugin_admin.py) mint one only after the account's owner/admin
rights AND the group's verification code check out, and call :func:`verify`
on every later request. :func:`verify` covers what the session itself
proves; the caller still checks that the account belongs to the session's
user and that the user is still an owner or admin, since those can change
without the session knowing.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

TOKEN_PREFIX = "dtp_"
#: A session unused for this long is over; using it slides the window.
IDLE_DAYS = 30
#: ``last_used_at`` is rewritten at most this often, not on every request.
TOUCH_EVERY = timedelta(minutes=5)

# Resolved lazily so this module imports without the ORM; tests set them.
PluginAdminSession = None
GroupVerificationCode = None


def _models():
    global PluginAdminSession, GroupVerificationCode
    if PluginAdminSession is None:
        from db.models import GroupVerificationCode as code_model
        from db.models import PluginAdminSession as session_model

        PluginAdminSession, GroupVerificationCode = session_model, code_model
    return PluginAdminSession, GroupVerificationCode


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_generation(s, group_id: int) -> Optional[int]:
    """The group's code generation while a code is active; None when the group
    has no active code (no session can be good then)."""
    _, code_model = _models()
    row = s.query(code_model).filter(code_model.group_id == int(group_id)).first()
    if row is None or not row.code_hash:
        return None
    return int(row.generation or 0)


def mint(s, group_id: int, user_id: int, account_hash: str, generation: int,
         now: Optional[datetime] = None) -> str:
    """A new session for this account on this group; returns the token, which
    is never stored. One live session per account and group: linking again
    revokes the previous one. Flushes but does not commit."""
    session_model, _ = _models()
    now = now or datetime.utcnow()
    for old in (s.query(session_model)
                .filter(session_model.group_id == int(group_id),
                        session_model.account_hash == str(account_hash),
                        session_model.revoked_at.is_(None))
                .all()):
        old.revoked_at, old.revoked_reason = now, "relink"
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    s.add(session_model(
        token_hash=hash_token(token), group_id=int(group_id), user_id=int(user_id),
        account_hash=str(account_hash), generation=int(generation), created_at=now, last_used_at=now))
    s.flush()
    return token


def bearer(header: Optional[str]) -> Optional[str]:
    """The token in an ``Authorization: Bearer dtp_...`` header, or None."""
    if not header:
        return None
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith(TOKEN_PREFIX) or len(token) > 100:
        return None
    return token


def verify(s, token: Optional[str], group_id: int, account_hash: str,
           now: Optional[datetime] = None) -> Tuple[Optional[object], str]:
    """``(session, "ok")`` when the token is a live session for this group and
    account under the group's current code, else ``(None, reason)`` with
    reason one of missing | unknown | revoked | group | account | code |
    expired. A session found expired is revoked on the spot."""
    if not token:
        return None, "missing"
    session_model, _ = _models()
    now = now or datetime.utcnow()
    row = s.query(session_model).filter(session_model.token_hash == hash_token(token)).first()
    if row is None:
        return None, "unknown"
    if row.revoked_at is not None:
        return None, "revoked"
    if int(row.group_id) != int(group_id):
        return None, "group"
    if str(row.account_hash) != str(account_hash):
        return None, "account"
    if current_generation(s, group_id) != int(row.generation):
        return None, "code"
    if (row.last_used_at or row.created_at) < now - timedelta(days=IDLE_DAYS):
        row.revoked_at, row.revoked_reason = now, "expired"
        s.flush()
        return None, "expired"
    if row.last_used_at is None or now - row.last_used_at > TOUCH_EVERY:
        row.last_used_at = now
        s.flush()
    return row, "ok"


def revoke(s, row, reason: str = "unlink", now: Optional[datetime] = None) -> None:
    row.revoked_at = now or datetime.utcnow()
    row.revoked_reason = reason
    s.flush()


def linked_group_ids(s, account_hash: str, user_id: int,
                     now: Optional[datetime] = None) -> set:
    """Groups this account holds a session for that would still verify."""
    session_model, code_model = _models()
    now = now or datetime.utcnow()
    rows = (s.query(session_model.group_id, session_model.generation)
            .join(code_model, code_model.group_id == session_model.group_id)
            .filter(session_model.account_hash == str(account_hash),
                    session_model.user_id == int(user_id),
                    session_model.revoked_at.is_(None),
                    session_model.last_used_at >= now - timedelta(days=IDLE_DAYS),
                    code_model.code_hash.isnot(None),
                    code_model.generation == session_model.generation)
            .all())
    return {int(r[0]) for r in rows}
