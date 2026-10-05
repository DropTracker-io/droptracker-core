"""Trusted "when did we actually receive this" stamps for server-side replays.

A submission recovered after an outage (the edge Worker's R2 spool, or the
Discord webhook channels the reader bot missed) should be dated when it first
reached us, not when the replay ran. Otherwise a three-hour outage books three
hours of kills into the recovery hour, or into the next month when the outage
crosses midnight on the 1st.

The intake normally refuses to believe an accept stamp older than 6 hours
(``data.submissions.common._RECEIVED_AT_MAX_LAG``), because an untrusted old
stamp could rewrite a closed month. A replay stamp is different: our own code
set it from a clock we control (the Worker's ``captured_at`` metadata, or
Discord's message snowflake), so it is believed for up to
``TRUSTED_RECEIVED_AT_MAX_LAG``.

Two ways a stamp becomes trusted:

* **In-process** (webhook reader catch-up): the payload is built by our own
  code from Discord's message timestamp, so it sets
  ``TRUSTED_FLAG`` directly. Client-supplied fields can never carry it: both
  intake paths strip every ``_received_at*`` key a client sends
  (:func:`strip_client_stamp_fields`).
* **Over HTTP** (R2 drain -> intake): the drain sends the stamp in
  ``X-DT-Received-At`` with an HMAC in ``X-DT-Received-Sig``. The acceptor only
  honours a stamp whose signature verifies; anything else is ignored and the
  submission is dated at accept time, exactly as before.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

STAMP_HEADER = "X-DT-Received-At"
SIG_HEADER = "X-DT-Received-Sig"

# Payload key processors read (see data.submissions.common.received_at).
TRUSTED_FLAG = "_received_at_trusted"

# How old a trusted stamp may be and still date the row. Past this the row is
# dated at processing time, like any other implausible stamp. A week covers a
# long weekend outage plus a slow recovery; R2 objects or Discord history older
# than that point at a broken pipeline someone should look at by hand.
TRUSTED_RECEIVED_AT_MAX_LAG = timedelta(days=7)

# Domain-separated so this signature can never double as a session token or
# anything else derived from the same secret.
_CONTEXT = b"droptracker:replay-received-at:v1"


def _key() -> Optional[bytes]:
    secret = (os.getenv("REPLAY_STAMP_KEY") or os.getenv("JWT_TOKEN_KEY") or "").strip()
    if not secret:
        return None
    return hmac.new(secret.encode(), _CONTEXT, hashlib.sha256).digest()


def normalize(value) -> Optional[str]:
    """ISO 8601 (any offset, or ``Z``) -> naive UTC ISO string, else None."""
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        stamped = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if stamped.tzinfo is not None:
        stamped = stamped.astimezone(timezone.utc).replace(tzinfo=None)
    return stamped.isoformat()


def sign(stamp: str) -> Optional[str]:
    """Signature for a normalized stamp, or None when no key is configured."""
    key = _key()
    stamp = normalize(stamp)
    if key is None or stamp is None:
        return None
    return hmac.new(key, stamp.encode(), hashlib.sha256).hexdigest()


def verify(stamp, signature) -> Optional[str]:
    """The normalized stamp when ``signature`` is valid for it, else None."""
    if not stamp or not signature:
        return None
    expected = sign(stamp)
    if expected is None:
        return None
    if not hmac.compare_digest(expected, str(signature).strip()):
        return None
    return normalize(stamp)


def strip_client_stamp_fields(data: dict) -> dict:
    """Drop any ``_received_at*`` key from client-built submission data.

    Embed fields become payload keys verbatim on both intake paths, so without
    this a client could date its own submission by adding a field named
    ``_received_at`` (and, with the trusted flag, up to a week back).
    """
    for key in [k for k in data if isinstance(k, str) and k.startswith("_received_at")]:
        data.pop(key, None)
    return data
