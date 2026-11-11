"""Read-only client for the OSRS Wiki's WikiSync player data.

WikiSync is a separate RuneLite plugin many players run; the wiki publishes
what it uploads at ``sync.runescape.wiki``. The one thing taken from it is
which achievement diary *tasks* a player has done, which our own sync does not
carry (see ``utils/diary_tasks.py`` for how it is cross-checked before use).

Best-effort by contract: every failure is ``None``, cached briefly so an
outage or an unknown player costs one request rather than one per page view.
Blocking (``requests``); call it from a worker thread.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BASE = "https://sync.runescape.wiki/runelite/player"
_TIMEOUT = 8
# A diary task is done at most a few times a day; the upload is no fresher
# than the player's last WikiSync submit anyway.
_HIT_TTL = 3 * 3600
_MISS_TTL = 30 * 60
_CACHE_PREFIX = "wikisync:diaries:"


def _cache():
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception:
        return None


def diaries_for(player_name: str) -> Optional[dict]:
    """``{"diaries": {...}, "timestamp": str|None}`` for a player, or None.

    ``diaries`` is WikiSync's ``achievement_diaries`` object: area name ->
    tier name -> ``{"complete": bool, "tasks": [bool, ...]}``.
    """
    name = (player_name or "").strip()
    if not name:
        return None
    key = _CACHE_PREFIX + name.lower()
    conn = _cache()
    if conn is not None:
        try:
            cached = conn.get(key)
            if cached:
                loaded = json.loads(cached)
                return loaded or None
        except Exception:
            pass

    result: Optional[dict] = None
    try:
        import requests

        from utils.external_calls import external_apis_allowed
        from utils.wiki_ua import USER_AGENT

        if external_apis_allowed():
            resp = requests.get(
                f"{_BASE}/{urllib.parse.quote(name)}/STANDARD",
                headers={"User-Agent": USER_AGENT},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                body: Any = resp.json()
                diaries = body.get("achievement_diaries") if isinstance(body, dict) else None
                if isinstance(diaries, dict) and diaries:
                    result = {"diaries": diaries, "timestamp": body.get("timestamp")}
    except Exception as exc:  # network, JSON, anything: the profile renders without it
        logger.info("WikiSync lookup failed for %r: %s", name, exc)

    if conn is not None:
        try:
            conn.setex(key, _HIT_TTL if result else _MISS_TTL, json.dumps(result or {}))
        except Exception:
            pass
    return result
