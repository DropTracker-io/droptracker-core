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
import os

import aiohttp

# Canonical on-disk location for item icons. Mirrors the paths hard-coded in
# lootboard/generator.py and web_api/routes/lootboard.py.
ITEMDB_DIR = "/store/droptracker/disc/static/assets/img/itemdb"

# RuneLite's item icon cache — the same source the lootboard generator uses.
RUNELITE_ICON_URL = "https://static.runelite.net/cache/item/icon/{item_id}.png"


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

    url = RUNELITE_ICON_URL.format(item_id=iid)
    owns_session = session is None
    try:
        if owns_session:
            session = aiohttp.ClientSession()
        async with session.get(url) as response:
            if response.status != 200:
                return False
            data = await response.read()
        if not data:
            return False
        os.makedirs(ITEMDB_DIR, exist_ok=True)
        # Write atomically so a concurrent reader never sees a partial file.
        tmp_path = f"{path}.tmp.{os.getpid()}"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
        return True
    except Exception:
        return False
    finally:
        if owns_session and session is not None:
            await session.close()


def ensure_item_image_sync(item_id) -> bool:
    """Blocking wrapper around :func:`ensure_item_image` for non-async callers."""
    return asyncio.run(ensure_item_image(item_id))
