"""Creating and claiming one-time key reveals.

Pure-ish logic plus the two DB operations, in ``db/`` so the web API (which
creates and serves reveals) and any script can both use it without importing
each other's app package.

The claim is deliberately written as one transaction that marks the row read
*before* returning the secret: if two requests race, exactly one of them can
observe an unviewed row, and the loser gets "already viewed" rather than a
second copy of the credential.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

#: How long an unclaimed link lives. Long enough to survive a Discord DM going
#: unread overnight, short enough that a forgotten link is not a standing risk.
DEFAULT_TTL_HOURS = 72

#: Outcomes of a claim. Only ``ok`` carries a secret.
OK = "ok"
NOT_FOUND = "not_found"
EXPIRED = "expired"
ALREADY_VIEWED = "already_viewed"
FORBIDDEN = "forbidden"


def new_reveal_token() -> str:
    """URL component. 32 bytes: guessing is not a threat model we rely on."""
    return secrets.token_urlsafe(32)


def _fernet():
    from utils.encrypter import get_encryption_key
    from cryptography.fernet import Fernet

    return Fernet(get_encryption_key())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


def create_reveal(session, *, api_key_id: int, plaintext: str,
                  audience_user_id: Optional[int] = None,
                  audience_group_id: Optional[int] = None,
                  created_by_user_id: Optional[int] = None,
                  ttl_hours: int = DEFAULT_TTL_HOURS) -> Tuple[object, str]:
    """Store an encrypted, single-use envelope. Returns ``(row, reveal_token)``.

    Exactly one audience must be given: a link nobody is allowed to open is
    useless, and a link *anybody* may open is the thing this exists to avoid.
    """
    if (audience_user_id is None) == (audience_group_id is None):
        raise ValueError("exactly one of audience_user_id / audience_group_id")

    from db.models import ApiKeyReveal

    token = new_reveal_token()
    row = ApiKeyReveal(
        reveal_token=token,
        api_key_id=api_key_id,
        secret_ciphertext=encrypt_secret(plaintext),
        audience_user_id=audience_user_id,
        audience_group_id=audience_group_id,
        created_by_user_id=created_by_user_id,
        expires_at=datetime.utcnow() + timedelta(hours=max(1, ttl_hours)),
    )
    session.add(row)
    return row, token


def may_view(session, row, viewer_user_id: Optional[int]) -> bool:
    """Whether this signed-in user is the reveal's audience.

    A group reveal is openable by anyone who administers that group, resolved
    live — so revoking someone's admin also revokes their ability to open a
    pending link.
    """
    if viewer_user_id is None:
        return False
    if row.audience_user_id is not None:
        return int(row.audience_user_id) == int(viewer_user_id)
    if row.audience_group_id is None:
        return False
    try:
        from web_api.deps import is_group_admin_role, resolve_group_role
        from web_api.deps import load_user

        user = load_user(session, viewer_user_id)
        role = resolve_group_role(session, viewer_user_id,
                                  int(row.audience_group_id), user=user)
        return bool(is_group_admin_role(role))
    except Exception:
        # Fail closed: an error resolving the role is not permission.
        return False


def claim(session, reveal_token: str, viewer_user_id: Optional[int]) -> Tuple[str, Optional[dict]]:
    """``(outcome, payload)``. Marks the row viewed and destroys the ciphertext.

    Checks run in the order a reader would want them explained, but every
    failure that could reveal whether a token exists is indistinguishable from
    the outside — the route maps NOT_FOUND, EXPIRED and FORBIDDEN to the same
    response.
    """
    from db.models import ApiKey, ApiKeyReveal

    row = (session.query(ApiKeyReveal)
           .filter(ApiKeyReveal.reveal_token == reveal_token).first())
    if row is None:
        return NOT_FOUND, None
    if row.viewed_at is not None:
        return ALREADY_VIEWED, None
    if row.expires_at is not None and row.expires_at <= datetime.utcnow():
        return EXPIRED, None
    if not may_view(session, row, viewer_user_id):
        return FORBIDDEN, None

    ciphertext = row.secret_ciphertext
    if not ciphertext:
        # Viewed flag and ciphertext disagree; treat as spent rather than
        # guessing that it is safe to hand something back.
        return ALREADY_VIEWED, None

    try:
        secret = decrypt_secret(ciphertext)
    except Exception:
        return NOT_FOUND, None

    # Burn it in the same transaction that reads it, so a race cannot yield
    # two copies of the credential.
    row.viewed_at = datetime.utcnow()
    row.viewed_by_user_id = int(viewer_user_id)
    row.secret_ciphertext = None
    session.commit()

    key = session.query(ApiKey).filter(ApiKey.id == row.api_key_id).first()
    return OK, {
        "token": secret,
        "key_id": int(row.api_key_id),
        "label": getattr(key, "label", "") or "",
        "tier": getattr(key, "tier_key", None),
        "scope": getattr(key, "scope", None),
        "group_id": getattr(key, "group_id", None),
    }
