"""Proof screenshots uploaded through the website (``POST /api/v1/uploads/proof``).

The upload route stores the image in B2 under a server-minted key,
``dt_uploads/{uuid4 hex}.{ext}``, and hands back the key and its public CDN
URL. Anything that later accepts one of these from a request must match the
key strictly, so a caller can never point a row at an arbitrary address.
"""
from __future__ import annotations

import os
import re
from typing import Optional

PROOF_KEY_RE = re.compile(r"^dt_uploads/[0-9a-f]{32}\.(?:png|jpg|webp|gif)$")


def _cdn_base() -> str:
    return (os.getenv("B2_CDN_BASE_URL") or "").rstrip("/")


def is_proof_key(key) -> bool:
    """Whether ``key`` is exactly a key the proof-upload route mints."""
    return isinstance(key, str) and bool(PROOF_KEY_RE.match(key))


def trusted_proof_url(url) -> Optional[str]:
    """The canonical public URL when ``url`` is one of our uploaded proof
    screenshots (CDN base + a minted key, nothing else), else None.

    The submission pipeline refuses client-supplied URLs on our own host
    (``data.submissions.common._safe_submitted_image_url``), which is right for
    the public intake but also discarded every website manual submission's
    proof (2026-11-22). This is the narrow exception: a stored object the
    website itself uploaded."""
    base = _cdn_base()
    if not base or not isinstance(url, str):
        return None
    prefix = base + "/"
    candidate = url.strip()
    if not candidate.startswith(prefix):
        return None
    key = candidate[len(prefix):]
    return prefix + key if is_proof_key(key) else None
