"""Recap card → PNG, and the on-disk artifact the Discord embed points at.

Mirrors :mod:`services.event_board_image`: build a URL for the chrome-less
render page, screenshot it with headless chromium over CDP
(:func:`services.page_screenshot.screenshot_url`), cache the bytes.

Two things differ from the board version, both because a recap is an *archive*
rather than a live view:

*Caching is trivial.* A board image is invalidated by a content signature over
mutable event state. A recap snapshot is immutable once written, so the cache
key is just its ``generated_at`` — regenerating the snapshot (after a backfill,
say) changes the stamp and the image follows.

*The PNG is written to disk, not only to Redis.* Discord delivery needs a public
URL: ``services/discord_outbox`` carries ``content`` + one embed and **has no
attachment column**, so a bot-side ``interactions.File`` upload is the only way
to send bytes — and that would confine posting to the bot process. Writing the
card under ``static/assets/img/clans/...`` instead gives an embed
``image.url`` (the lootboard already does exactly this), which means web_api, a
script, or an admin button can all trigger a post. The file's existence
doubles as the "have I already posted this period?" watermark, the same
mtime-as-state trick ``lootboard/board_generator`` uses.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote

from db.app_logger import AppLogger
from db.models.recap import SCOPE_GROUP, SCOPE_PLAYER

app_logger = AppLogger()

# Same origin as the board export. Must stay the public site: the card's item
# and NPC icons resolve from site-relative `/img/itemdb/{id}.png`, which nginx
# serves off the backend's static tree — pointing this at a bare
# 127.0.0.1:3138x would 404 every icon.
RECAP_IMAGE_BASE_URL = os.getenv(
    "BOARD_IMAGE_BASE_URL", "https://www.droptracker.io"
).rstrip("/")

# CSS layout width of the render page — keep in sync with WIDTH in
# apps/web/app/recap-image/[scope]/[id]/[period]/page.tsx.
RECAP_IMAGE_WIDTH = 1100
RECAP_IMAGE_SCALE = 2.0

_CACHE_TTL_SECONDS = 24 * 3600

# Where the rendered card lands. Group cards sit beside that group's lootboard
# so the existing nginx /img mapping serves them with no new config.
_CLAN_ASSET_ROOT = "/store/droptracker/disc/static/assets/img/clans"
_PUBLIC_IMG_BASE = "https://www.droptracker.io/img/clans"


def _render_token() -> str:
    return os.environ.get("BOARD_IMAGE_TOKEN", "")


def recap_image_page_url(scope: str, subject_id: int, period: str) -> str:
    """The chrome-less render page chromium screenshots."""
    return (
        f"{RECAP_IMAGE_BASE_URL}/recap-image/{scope}/{int(subject_id)}/{period}"
        f"?k={quote(_render_token(), safe='')}"
    )


def recap_image_path(scope: str, subject_id: int, period: str) -> str:
    """Absolute path of the stored card."""
    return os.path.join(
        _CLAN_ASSET_ROOT, str(int(subject_id)), "recap", f"{scope}-{period}.png"
    )


def recap_image_url(scope: str, subject_id: int, period: str) -> str:
    """Public URL of the stored card — what a Discord embed's ``image.url``
    points at."""
    return f"{_PUBLIC_IMG_BASE}/{int(subject_id)}/recap/{scope}-{period}.png"


def recap_image_exists(scope: str, subject_id: int, period: str) -> bool:
    """Whether the card has already been rendered.

    This is the delivery cycle's idempotency check: it survives restarts, needs
    no Redis key and no extra table, and self-heals if someone deletes the file
    to force a re-render.
    """
    return os.path.exists(recap_image_path(scope, subject_id, period))


async def render_recap_png(
    scope: str, subject_id: int, period: str, *, scale: float = RECAP_IMAGE_SCALE
) -> Optional[bytes]:
    """Screenshot the render page. ``None`` on any failure — a card that won't
    render must never take down the message it was going to illustrate."""
    token = _render_token()
    if not token:
        app_logger.log(
            log_type="error",
            data="BOARD_IMAGE_TOKEN unset; recap rendering disabled",
            app_name="core",
            description="render_recap_png",
        )
        return None

    url = recap_image_page_url(scope, subject_id, period)
    try:
        from services.page_screenshot import screenshot_url

        return await screenshot_url(url, width=RECAP_IMAGE_WIDTH, scale=scale)
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"recap render failed for {scope}/{subject_id}/{period}: {e}",
            app_name="core",
            description="render_recap_png",
        )
        return None


async def recap_png_cached(
    scope: str, subject_id: int, period: str, generated_at: str
) -> Optional[bytes]:
    """Rendered bytes, memoised on the snapshot's ``generated_at`` stamp.

    A snapshot is immutable once written, so the stamp is a complete cache key:
    it only moves when the payload is regenerated, and the image should move
    with it.
    """
    hash_key = f"recap:{scope}:{subject_id}:{period}:stamp"
    png_key = f"recap:{scope}:{subject_id}:{period}:png"

    try:
        # Raw client for the PNG: the wrapper's get() utf-8-decodes and would
        # corrupt the bytes.
        from utils.redis import redis_client

        if redis_client.get(hash_key) == generated_at:
            cached = redis_client.client.get(png_key)
            if cached:
                return bytes(cached)
    except Exception:
        pass  # best-effort; fall through and render

    png = await render_recap_png(scope, subject_id, period)
    if not png:
        return None

    try:
        from utils.redis import redis_client

        redis_client.client.setex(png_key, _CACHE_TTL_SECONDS, png)
        redis_client.setex(hash_key, _CACHE_TTL_SECONDS, generated_at)
    except Exception:
        pass  # a write miss just means we render again next time
    return png


async def write_recap_image(
    scope: str, subject_id: int, period: str, generated_at: str
) -> Optional[str]:
    """Render and persist the card, returning its public URL (``None`` on
    failure).

    Group cards go under the group's own asset directory; player cards are
    filed under the player id in the same tree, which keeps one nginx mapping
    rather than two.

    Directory mode is 0o777 deliberately: the bots run as ``User=user`` and
    ``droptracker-webapi`` as ``User=debian``, and any shared asset tree either
    side may write has to stay group-writable or one of them silently fails.
    """
    if scope not in (SCOPE_GROUP, SCOPE_PLAYER):
        return None

    png = await recap_png_cached(scope, subject_id, period, generated_at)
    if not png:
        return None

    path = recap_image_path(scope, subject_id, period)
    try:
        os.makedirs(os.path.dirname(path), mode=0o777, exist_ok=True)
        # Write-then-rename so a reader (or nginx) never sees a half-written
        # PNG, and so a crashed render can't leave a truncated file that the
        # existence check would then treat as "already done".
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, path)
        os.chmod(path, 0o666)
    except OSError as e:
        app_logger.log(
            log_type="error",
            data=f"could not write recap image {path}: {e}",
            app_name="core",
            description="write_recap_image",
        )
        return None

    return recap_image_url(scope, subject_id, period)
