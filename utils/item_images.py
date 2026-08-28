"""On-demand OSRS item icon fetching.

Item icons live at ``static/assets/img/itemdb/{id}.png`` and are served to the
website as ``{IMG_BASE}/itemdb/{id}.png`` (via the :8080 image server / nginx
``/img/`` proxy). Historically only the lootboard PNG generator
(:func:`lootboard.generator.load_rl_cache_img`) downloaded missing icons; the
website read paths and the submission ingest path did not. As a result an item
could be tracked in the DB yet render as the placeholder GIF indefinitely.

This module centralises that download so every code path that needs an icon
can guarantee it exists with a single call. It is deliberately dependency-light
(only ``aiohttp`` + stdlib) so it can be imported from the ingest path, the
image server, and one-off backfill scripts without pulling in Pillow or the
lootboard package.
"""

import asyncio
import base64
import logging
import os
import time

import aiohttp

logger = logging.getLogger(__name__)

# A 1x1 fully transparent PNG, served in place of an item icon we cannot get.
#
# What this replaces: the image server used to answer a missing icon with
# `droptracker-small.gif`, a 672 KB animated logo, at HTTP 200. Every surface
# that draws items — the equipment panel, the loot tracker, item pickers —
# therefore rendered a large branded blob where a 400-byte sprite belonged, and
# because the status said success the frontend's onError could never intervene.
#
# Transparent rather than a drawn "unknown item" glyph because this composites
# over surfaces that already say the right thing: the equipment panel draws its
# own stone slot tile underneath, and a list row keeps its own spacing. A drawn
# placeholder would fight all of them. 1x1 also gives the frontend a reliable
# marker (naturalWidth === 1) since real icons are 36x32 — an <img> cannot read
# response headers, so the body has to carry the signal.
TRANSPARENT_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Header naming the response a fallback, for consumers that CAN read headers
# (Discord's unfurler, monitoring, curl). The status code carries the same
# meaning; this makes it greppable in logs without decoding the body.
PLACEHOLDER_HEADER = "X-DT-Placeholder"

# A failure response is never cacheable. This is not belt-and-braces — it is
# the fix for the half of the reported bug that lived at the edge: the old
# placeholder went out as `max-age=43200`, Cloudflare's zone-level Browser Cache
# TTL rewrote it to `max-age=86400`, and the CDN then served a 672 KB GIF for a
# further day against item ids whose icons the origin had already repaired.
#
# `max-age=60` is not enough, because that same zone override rewrites any
# positive max-age upward (measured: 60 -> 1800). `no-store` is the only value
# it leaves alone, and it is what this response actually means. The cost is that
# every request for a genuinely missing icon reaches the origin — which is fine,
# because the negative cache below makes that answer a dict lookup (~2 ms, 68
# bytes) rather than a round trip to RuneLite.
PLACEHOLDER_CACHE_CONTROL = "no-store"

# Ids RuneLite has already told us it has no icon for, and when to ask again.
# Without this every page view of a genuinely-iconless item costs an outbound
# HTTPS round trip to static.runelite.net before we can answer, which is the
# "repeated misses are cheap" requirement — a board full of unknown items would
# otherwise hammer them once per tile per view.
_NEGATIVE_TTL_SECONDS = 3600
_MAX_NEGATIVE_ENTRIES = 20_000
_negative_cache: dict[int, float] = {}


def _negatively_cached(item_id: int) -> bool:
    expires = _negative_cache.get(item_id)
    if expires is None:
        return False
    if expires <= time.monotonic():
        _negative_cache.pop(item_id, None)
        return False
    return True


def _remember_missing(item_id: int) -> None:
    # Bounded: this is a long-lived process, and an unbounded dict keyed by
    # anything a client can name is a slow memory leak. Dropping the whole map
    # is fine — the worst case is one extra fetch attempt per id afterwards.
    if len(_negative_cache) >= _MAX_NEGATIVE_ENTRIES:
        _negative_cache.clear()
    _negative_cache[item_id] = time.monotonic() + _NEGATIVE_TTL_SECONDS



# Canonical on-disk location for item icons. Mirrors the paths hard-coded in
# lootboard/generator.py and web_api/routes/lootboard.py.
ITEMDB_DIR = "/store/droptracker/disc/static/assets/img/itemdb"

# RuneLite's item icon cache — the same source the lootboard generator uses.
RUNELITE_ICON_URL = "https://static.runelite.net/cache/item/icon/{item_id}.png"

# How many icons to download at once in :func:`ensure_item_images`. Small on
# purpose: this runs on the ingest path, and RuneLite's cache is a courtesy.
DEFAULT_CONCURRENCY = 8


def ensure_public_dir(path: str) -> None:
    """Create a directory that BOTH service accounts can write.

    The bots, intake API and workers run as ``user``; ``droptracker-webapi``,
    the node units and every hand-run backfill script run as ``debian``. A
    directory created 0755 by one account is silently unwritable by the other,
    and because every writer here treats a failed icon fetch as a soft failure,
    that shows up not as an error but as items that render as the placeholder
    GIF forever. This is the same trap ``services/player_model.py`` documents.
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o777)
    except OSError:
        # Not ours to chmod (already correct, or owned by the other account
        # with the right mode). Nothing to do — the write below will tell us.
        pass


def item_image_path(item_id) -> str:
    """Absolute path to the icon PNG for ``item_id`` (whether or not it exists)."""
    return os.path.join(ITEMDB_DIR, f"{int(item_id)}.png")


def item_image_exists(item_id) -> bool:
    try:
        return os.path.exists(item_image_path(item_id))
    except (TypeError, ValueError):
        return False


async def ensure_item_image(item_id, session: "aiohttp.ClientSession | None" = None) -> bool:
    """Ensure the icon for ``item_id`` is present on disk, downloading if needed.

    Returns True when the file exists on disk afterwards (already present or
    freshly downloaded), False otherwise. Never raises — callers treat a
    missing icon as a soft failure (the image server falls back to a
    placeholder), so a transient network error must not break ingest.
    """
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return False
    # RuneLite has no icon for negative/sentinel ids (e.g. -1 "no item").
    if iid < 0:
        return False

    path = item_image_path(iid)
    if os.path.exists(path):
        return True

    # Already known to be unavailable upstream. Answering straight away is the
    # difference between a placeholder costing a dict lookup and it costing an
    # HTTPS round trip on every single page view.
    if _negatively_cached(iid):
        return False

    url = RUNELITE_ICON_URL.format(item_id=iid)
    owns_session = session is None
    try:
        if owns_session:
            session = aiohttp.ClientSession()
        async with session.get(url) as response:
            if response.status != 200:
                # 404 is definitive: RuneLite has no icon for plenty of ids
                # (placeholders, Leagues variants), and that will not change
                # until the next cache build, so stop asking. Any other status
                # is the server having a bad moment — retry that one, or a
                # blip would blank an icon for the whole TTL.
                if response.status == 404:
                    _remember_missing(iid)
                return False
            data = await response.read()
        if not data:
            _remember_missing(iid)
            return False
        ensure_public_dir(ITEMDB_DIR)
        # Write atomically so a concurrent reader never sees a partial file.
        tmp_path = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            # 0666 so the other service account can replace it later; the file
            # inherits the writer's umask otherwise and we are back to the
            # cross-account trap ensure_public_dir exists to avoid.
            try:
                os.chmod(tmp_path, 0o666)
            except OSError:
                pass
            os.replace(tmp_path, path)
        except OSError as exc:
            # THE failure that matters. A missing icon degrades to a placeholder
            # silently, so an unwritable directory is invisible until someone
            # notices half the item icons on the site are wrong. Say so.
            logger.warning(
                "Could not write item icon %s to %s: %s", iid, ITEMDB_DIR, exc
            )
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
        return True
    except Exception as exc:
        logger.debug("Item icon fetch failed for %s: %s", iid, exc)
        return False
    finally:
        if owns_session and session is not None:
            await session.close()


async def ensure_item_images(item_ids, concurrency: int = DEFAULT_CONCURRENCY) -> int:
    """Ensure icons exist for every id in ``item_ids``; returns how many were fetched.

    Ids already on disk cost a single ``os.path.exists`` and no network call, so
    calling this on the ingest path for a full worn-equipment set is free in the
    steady state and only pays for genuinely new items. Never raises.
    """
    try:
        wanted = {int(i) for i in item_ids}
    except (TypeError, ValueError):
        return 0
    missing = [i for i in wanted if i >= 0 and not os.path.exists(item_image_path(i))]
    if not missing:
        return 0

    fetched = 0
    try:
        semaphore = asyncio.Semaphore(max(1, concurrency))
        async with aiohttp.ClientSession() as session:

            async def one(iid):
                async with semaphore:
                    return await ensure_item_image(iid, session=session)

            results = await asyncio.gather(
                *(one(i) for i in missing), return_exceptions=True
            )
        fetched = sum(1 for r in results if r is True)
    except Exception as exc:
        logger.debug("Batch item icon fetch failed: %s", exc)
    return fetched


def ensure_item_image_sync(item_id) -> bool:
    """Blocking wrapper around :func:`ensure_item_image` for non-async callers."""
    return asyncio.run(ensure_item_image(item_id))


# Grayscale (desaturated) variants of item icons, served as
# ``{IMG_BASE}/itemdb/gray/{id}.png``. The Loot Sweep board renders hundreds of
# "not yet received" receipt tabs as greyed icons; it used to do that with a
# per-element CSS ``filter: grayscale(100%)``, which the browser re-rasterises
# every time a row scrolls into view (the desktop scroll-jank culprit). Baking
# the grayscale into a static PNG once moves that cost off every website client.
# Backfilled by scripts/generate_grayscale_icons.py and self-healed on demand by
# web/front.py's image route.
GRAY_DIR = os.path.join(ITEMDB_DIR, "gray")


def gray_image_path(item_id) -> str:
    """Absolute path to the grayscale icon PNG for ``item_id`` (may not exist)."""
    return os.path.join(GRAY_DIR, f"{int(item_id)}.png")


def ensure_grayscale_variant(item_id) -> bool:
    """Ensure a grayscale variant of the icon exists on disk.

    Desaturates ``itemdb/{id}.png`` (luminance, alpha preserved) into
    ``itemdb/gray/{id}.png``. Requires the colour source to already exist —
    callers that might be missing it should ``await ensure_item_image`` first.
    Returns True when the grayscale file is present afterwards. Never raises —
    a failure just means the client falls back to the colour icon.
    """
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return False
    if iid < 0:
        return False
    dst = gray_image_path(iid)
    if os.path.exists(dst):
        return True
    src = item_image_path(iid)
    if not os.path.exists(src):
        return False
    try:
        # Lazy import so this module stays Pillow-free for the ingest path that
        # only ever calls ensure_item_image.
        from PIL import Image

        with Image.open(src) as im:
            im = im.convert("RGBA")
            lum = im.convert("L")
            out = Image.merge("RGBA", (lum, lum, lum, im.getchannel("A")))
        ensure_public_dir(GRAY_DIR)
        # Write atomically so a concurrent reader never sees a partial file.
        tmp_path = f"{dst}.tmp.{os.getpid()}"
        out.save(tmp_path, "PNG")
        try:
            os.chmod(tmp_path, 0o666)
        except OSError:
            pass
        os.replace(tmp_path, dst)
        return True
    except OSError as exc:
        logger.warning(
            "Could not write grayscale icon %s to %s: %s", iid, GRAY_DIR, exc
        )
        return False
    except Exception as exc:
        logger.debug("Grayscale variant failed for %s: %s", iid, exc)
        return False
