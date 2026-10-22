"""Group verification codes: issuing, clearing and checking them.

The code is the second factor for administering a group from the RuneLite
plugin (the first is that the player's account is linked to a user who owns or
administers the group). See ``db/models/group_verification_code.py`` for the
table.

Rules the routes rely on:

* The plaintext is returned once, by :func:`issue_code`, and never stored.
  Nobody can read it back: not the owner, not staff. A lost code is replaced,
  never recovered.
* Only a keyed hash is stored: HMAC-SHA256 over ``"<group_id>:<digits>"``.
  Nine digits is a small space, so a plain hash leaked with the database would
  fall to brute force in seconds; the key lives in the environment, not the
  database. Binding the group id means equal codes in two groups never match
  each other.
* :func:`check_code` is the only way to test a code, and it is throttled per
  caller and per group, so the 10^9 space cannot be walked online either.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime
from typing import Optional

# Resolved lazily (see _model) so this module imports without the ORM, the way
# db/api_key_reveals.py does; tests set it to their own mapped class.
GroupVerificationCode = None


def _model():
    global GroupVerificationCode
    if GroupVerificationCode is None:
        from db.models import GroupVerificationCode as model

        GroupVerificationCode = model
    return GroupVerificationCode

CODE_DIGITS = 9
HASH_VERSION = "v1"

# Wrong guesses allowed per caller (account hash / IP) and per group before
# further attempts are refused, and how long a window lasts. The per-group cap
# is higher so one noisy caller cannot lock a group's real admins out.
SUBJECT_FAILURES_MAX = 5
GROUP_FAILURES_MAX = 30
FAILURE_WINDOW_SECONDS = 15 * 60


class CodeKeyMissing(RuntimeError):
    """No key is configured to hash codes with; refuse rather than hash weakly."""


# Which environment variable keyed a stored hash. Recorded in the hash itself so
# adding a dedicated GROUP_CODE_PEPPER later does not silently invalidate every
# code issued under the fallback.
_KEY_SOURCES = {
    "p": "GROUP_CODE_PEPPER",
    "j": "JWT_TOKEN_KEY",
    "e": "ENCRYPTION_KEY",
}


def _current_key_source() -> str:
    for source, env in _KEY_SOURCES.items():
        if os.getenv(env):
            return source
    raise CodeKeyMissing("Set GROUP_CODE_PEPPER (or JWT_TOKEN_KEY) to issue codes.")


def _hmac(source: str, group_id: int, digits: str) -> str:
    env = _KEY_SOURCES.get(source)
    secret = os.getenv(env) if env else None
    if not secret:
        raise CodeKeyMissing(f"{env or source} is not set.")
    # Domain-separate from every other use of the same secret.
    key = hmac.new(secret.encode(), b"droptracker/group-verification-code", hashlib.sha256).digest()
    return hmac.new(key, f"{int(group_id)}:{digits}".encode(), hashlib.sha256).hexdigest()


def hash_code(group_id: int, digits: str) -> str:
    source = _current_key_source()
    return f"{HASH_VERSION}:{source}:{_hmac(source, group_id, digits)}"


def generate_digits() -> str:
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def format_code(digits: str) -> str:
    """``123456789`` -> ``123-456-789``."""
    return "-".join(digits[i:i + 3] for i in range(0, len(digits), 3))


def normalize_code(raw) -> Optional[str]:
    """Accept ``123-456-789``, ``123 456 789`` or ``123456789``; else None."""
    if raw is None:
        return None
    digits = re.sub(r"[\s\-]", "", str(raw))
    # ASCII only: str.isdigit() also accepts full-width and other script digits.
    if not re.fullmatch(r"[0-9]{%d}" % CODE_DIGITS, digits):
        return None
    return digits


def matches(stored: Optional[str], group_id: int, digits: str) -> bool:
    if not stored or not digits:
        return False
    parts = stored.split(":", 2)
    if len(parts) != 3 or parts[0] != HASH_VERSION:
        return False
    try:
        candidate = _hmac(parts[1], group_id, digits)
    except CodeKeyMissing:
        return False
    return hmac.compare_digest(candidate, parts[2])


# --------------------------------------------------------------------------- #
# DB operations (caller owns the session and commits)
# --------------------------------------------------------------------------- #
def get_row(s, group_id: int, *, for_update: bool = False):
    model = _model()
    q = s.query(model).filter(model.group_id == int(group_id))
    if for_update:
        q = q.with_for_update()
    return q.first()


def status(s, group_id: int) -> dict:
    """What the site may show about a group's code. Never the code itself."""
    row = get_row(s, group_id)
    active = bool(row and row.code_hash)
    return {
        "active": active,
        "issued_at": row.issued_at.isoformat() if active and row.issued_at else None,
        "issued_by_user_id": row.issued_by_user_id if active else None,
        "issued_via": row.issued_via if active else None,
        "cleared_at": row.cleared_at.isoformat() if row and not active and row.cleared_at else None,
        "cleared_reason": row.cleared_reason if row and not active else None,
    }


def issue_code(s, group_id: int, actor_user_id: Optional[int], via: str,
               *, row=None) -> str:
    """Store a fresh code for the group and return it, formatted. Shown once.

    Replaces any active code and bumps the generation, which revokes whatever
    was minted from the old one. Flushes but does not commit.
    """
    digits = generate_digits()
    code_hash = hash_code(group_id, digits)
    if row is None:
        row = get_row(s, group_id, for_update=True)
    if row is None:
        row = _model()(group_id=int(group_id), generation=0)
        s.add(row)
    row.code_hash = code_hash
    row.generation = int(row.generation or 0) + 1
    row.issued_via = via
    row.issued_at = datetime.utcnow()
    row.issued_by_user_id = actor_user_id
    s.flush()
    return format_code(digits)


def clear_code(s, group_id: int, actor_user_id: Optional[int], reason: str) -> bool:
    """Withdraw the active code without a replacement. True if one was active.

    The owner can then reveal a new one on the site. Bumps the generation for
    the same reason as :func:`issue_code`.
    """
    row = get_row(s, group_id, for_update=True)
    if row is None or not row.code_hash:
        return False
    row.code_hash = None
    row.generation = int(row.generation or 0) + 1
    row.cleared_at = datetime.utcnow()
    row.cleared_by_user_id = actor_user_id
    row.cleared_reason = reason
    s.flush()
    return True


# --------------------------------------------------------------------------- #
# Checking a code (throttled)
# --------------------------------------------------------------------------- #
def _subject_key(subject: str) -> str:
    return f"gvc:fail:s:{subject}"


def _group_key(group_id: int) -> str:
    return f"gvc:fail:g:{int(group_id)}"


def _count(rc, key: str) -> int:
    try:
        return int(rc.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _record_failure(rc, group_id: int, subject: str) -> None:
    pipe = rc.pipeline()
    for key in (_subject_key(subject), _group_key(group_id)):
        pipe.incr(key)
        # NX: the window runs from the first failure, so steady guessing
        # cannot keep extending it past the cap.
        pipe.expire(key, FAILURE_WINDOW_SECONDS, nx=True)
    pipe.execute()


def check_code(s, rc, group_id: int, raw_code, subject: str) -> str:
    """Test a code for a group. Returns one of:

    ``ok`` - matches the active code; ``wrong`` - does not (counted);
    ``invalid`` - not nine digits (not counted, it was never a guess);
    ``none`` - the group has no active code; ``throttled`` - too many recent
    failures for this caller or this group.

    ``rc`` is a raw redis client. Without one the check fails closed.
    ``subject`` identifies the caller, e.g. ``"acc:<hash>"``.
    """
    if rc is None:
        return "throttled"
    if (_count(rc, _subject_key(subject)) >= SUBJECT_FAILURES_MAX
            or _count(rc, _group_key(group_id)) >= GROUP_FAILURES_MAX):
        return "throttled"
    digits = normalize_code(raw_code)
    if digits is None:
        return "invalid"
    row = get_row(s, group_id)
    if row is None or not row.code_hash:
        return "none"
    if matches(row.code_hash, group_id, digits):
        return "ok"
    _record_failure(rc, group_id, subject)
    return "wrong"
