"""Render a group's Clan Log summary card to a PNG for Discord.

Same shape as :mod:`services.event_board_image`: screenshot the real web card
(``/clan-log-image/{group}/{period}``) with headless chromium so the posted
image cannot drift from the page its button links to, and cache the bytes in
Redis against a **state signature** so an unchanged board is never re-shot.

What is deliberately *not* rendered is the whole grid. ~350 slots across ~60
sections is taller than :func:`services.page_screenshot.screenshot_url`'s
8000px ceiling and unreadable in an embed anyway, so the card carries the
completion dial, the per-category bars, the latest unlocks and a sample of
what's left — and the button opens the interactive board.

Fully fail-open: any error returns ``None`` and the caller posts without an
image.

Config (env): ``BOARD_IMAGE_TOKEN`` (shared secret gating the export route;
unset ⇒ disabled), ``BOARD_IMAGE_BASE_URL`` (origin chromium hits — must stay a
public origin, because the card's item icons are site-relative ``/img/...``
paths served off the backend's static tree).
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional
from urllib.parse import quote

from db.app_logger import AppLogger

app_logger = AppLogger()

BOARD_IMAGE_BASE_URL = os.getenv(
    "BOARD_IMAGE_BASE_URL", "https://www.droptracker.io"
).rstrip("/")

# CSS layout width of the export page — keep in sync with WIDTH in
# apps/web/app/clan-log-image/[id]/[period]/page.tsx.
CLAN_LOG_IMAGE_WIDTH = 1100
CLAN_LOG_IMAGE_SCALE = 2.0

# The card is a fixed composition, so a short viewport crops it exactly rather
# than padding the PNG out to a full window height.
_VIEWPORT_HEIGHT = 400
_CACHE_TTL_SECONDS = 6 * 3600


def _token() -> str:
    return os.environ.get("BOARD_IMAGE_TOKEN", "")


def clan_log_image_page_url(group_id: int, period: str) -> str:
    return (
        f"{BOARD_IMAGE_BASE_URL}/clan-log-image/{int(group_id)}/"
        f"{quote(str(period), safe='')}?k={quote(_token(), safe='')}"
    )


def board_state_hash(payload: dict) -> str:
    """What must change before the card is worth re-shooting.

    Only the things the *card* shows: the headline counts, the catalog it was
    scored against, and the latest unlocks. A board whose grid changed
    somewhere the card doesn't display still re-renders via ``generated_at``,
    which the refresh task only moves when the ledger actually changed.
    """
    summary = payload.get("summary") or {}
    recent = payload.get("recent") or []
    raw = "|".join(
        [
            str(payload.get("group_id")),
            str(payload.get("period")),
            str(payload.get("catalog_version")),
            str(payload.get("generated_at")),
            f"{summary.get('obtained')}/{summary.get('total')}",
            ",".join(f"{r.get('item_id')}@{r.get('at')}" for r in recent[:5]),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def screenshot_clan_log(group_id: int, period: str, *,
                              scale: float = CLAN_LOG_IMAGE_SCALE) -> Optional[bytes]:
    """Screenshot the export page → PNG bytes, or ``None`` (fail-open)."""
    if not _token():
        app_logger.log(
            log_type="warning",
            data="BOARD_IMAGE_TOKEN is unset — Clan Log images are disabled.",
            app_name="clan_log_image", description="screenshot_clan_log")
        return None
    from services.page_screenshot import screenshot_url

    try:
        return await screenshot_url(
            clan_log_image_page_url(group_id, period),
            width=CLAN_LOG_IMAGE_WIDTH, scale=scale,
            viewport_height=_VIEWPORT_HEIGHT,
        )
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"Clan Log screenshot failed for group {group_id} ({period}): {e}",
            app_name="clan_log_image", description="screenshot_clan_log")
        return None


async def clan_log_image_with_hash(session, group_id: int, period: str = "all"):
    """``(png_bytes, state_hash, rendered)`` for one group's card.

    ``state_hash`` lets a caller keep a last-posted marker and skip a Discord
    edit when nothing changed; ``rendered`` is True only when a real screenshot
    ran, so a per-tick render budget counts shots rather than cache hits.
    """
    from services.clan_log import load_board

    try:
        payload = load_board(session, group_id, period)
    except Exception as e:
        app_logger.log(
            log_type="error", data=f"Clan Log board read failed for {group_id}: {e}",
            app_name="clan_log_image", description="clan_log_image_with_hash")
        return None, None, False
    if not payload:
        return None, None, False

    state_hash = board_state_hash(payload)
    cache_key = f"clan_log:{int(group_id)}:{period}:img:{state_hash}"

    conn = _redis()
    if conn is not None:
        try:
            cached = conn.get(cache_key)
            if cached:
                return cached, state_hash, False
        except Exception:
            pass

    png = await screenshot_clan_log(group_id, period)
    if not png:
        return None, state_hash, False

    if conn is not None:
        try:
            conn.setex(cache_key, _CACHE_TTL_SECONDS, png)
        except Exception:
            pass
    return png, state_hash, True


async def clan_log_image_png(session, group_id: int, period: str = "all") -> Optional[bytes]:
    png, _hash, _rendered = await clan_log_image_with_hash(session, group_id, period)
    return png


def _redis():
    """The raw redis-py client.

    ``redis_client`` itself is the app's string-shaped wrapper; PNG bytes have
    to go through ``.client`` or they come back mangled — the same reason
    ``event_board_image`` caches its images there.
    """
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception:
        return None
