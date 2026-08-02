"""Local mirroring of group icons.

``groups.icon_url`` is a free-form column: it usually holds a Discord CDN URL
for the guild icon, and it is editable from the admin data browser, so it can
point anywhere. The RuneLite plugin is not permitted to fetch it.

Plugin Hub rule (from the maintainers): *"all URLs that the plugin talks to MUST
either be hardcoded in the plugin, or directly input by the user, without
exception"* — fetching a URL that arrived in an API response is an SSRF risk and
makes the set of domains the plugin contacts unreviewable. So the API hands the
plugin a **path** under ``/img/`` and the plugin anchors it onto its own
hardcoded host. That only works if the bytes are actually on our host, hence
this mirror.

Icons land at ``static/assets/img/clans/{group_id}/icon.png``, matching the
existing ``clans/{group_id}/lb/lootboard.png`` convention.
"""

import asyncio
import os
import time

import aiohttp

from utils.plugin_urls import IMG_BASE_PREFIXES, discord_invite_code, img_relative

__all__ = [
    "CLANS_DIR",
    "discord_invite_code",
    "ensure_group_icon",
    "ensure_group_icon_sync",
    "icon_mirror_path",
    "icon_relative_path",
]

CLANS_DIR = "/store/droptracker/disc/static/assets/img/clans"

# Discord icons are PNG/GIF/WebP and small; cap generously and bail on anything
# that looks like it is not an icon.
MAX_ICON_BYTES = 2 * 1024 * 1024

# How long a failed mirror attempt is remembered. ensure_group_icon runs on every
# /group_search, so without this a group whose icon 404s (deleted guild icon, dead
# CDN link) would re-attempt the download — and pay its timeout — on every single
# request, forever.
FAILURE_TTL_SECONDS = 30 * 60
_recent_failures: dict = {}


def _failed_recently(url) -> bool:
    expires_at = _recent_failures.get(url)
    if expires_at is None:
        return False
    if expires_at > time.time():
        return True
    _recent_failures.pop(url, None)
    return False


def _remember_failure(url) -> None:
    now = time.time()
    if len(_recent_failures) > 512:
        for stale, expires_at in list(_recent_failures.items()):
            if expires_at <= now:
                _recent_failures.pop(stale, None)
    _recent_failures[url] = now + FAILURE_TTL_SECONDS


def icon_mirror_path(group_id) -> str:
    """Absolute path to the mirrored icon for ``group_id`` (may not exist)."""
    return os.path.join(CLANS_DIR, str(int(group_id)), "icon.png")


def icon_relative_path(group_id, icon_url):
    """Path under ``/img/`` the plugin should load, or None if there is none.

    Returns the already-local path when ``icon_url`` points at our own image
    host, otherwise the mirrored path once :func:`ensure_group_icon` has
    produced it. Returns None while a remote icon has not been mirrored yet —
    the plugin renders no icon in that case, exactly as it does for a group that
    never set one.
    """
    local = img_relative(icon_url)
    if local:
        return local
    if not icon_url:
        return None
    try:
        if os.path.exists(icon_mirror_path(group_id)):
            return f"clans/{int(group_id)}/icon.png"
    except (TypeError, ValueError):
        return None
    return None


async def ensure_group_icon(group_id, icon_url, session=None) -> bool:
    """Mirror a remote group icon to disk if it isn't there already.

    Returns True when a usable local copy exists afterwards. Never raises: a
    missing icon is a cosmetic failure and must not break the group endpoint.
    """
    if not icon_url:
        return False
    url = str(icon_url).strip()
    if any(url.startswith(prefix) for prefix in IMG_BASE_PREFIXES):
        return True
    if not url.startswith("https://"):
        return False
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return False

    path = icon_mirror_path(gid)
    if os.path.exists(path):
        return True
    if _failed_recently(url):
        return False

    owns_session = session is None
    try:
        if owns_session:
            session = aiohttp.ClientSession()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status != 200:
                _remember_failure(url)
                return False
            data = await response.read()
        if not data or len(data) > MAX_ICON_BYTES:
            _remember_failure(url)
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write atomically so a concurrent reader never sees a partial file.
        tmp_path = f"{path}.tmp.{os.getpid()}"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
        return True
    except Exception:
        _remember_failure(url)
        return False
    finally:
        if owns_session and session is not None:
            await session.close()


def ensure_group_icon_sync(group_id, icon_url) -> bool:
    """Blocking wrapper around :func:`ensure_group_icon` for non-async callers."""
    return asyncio.run(ensure_group_icon(group_id, icon_url))


# Strong references to in-flight mirror tasks. asyncio only holds a weak
# reference to a running task, so without this the GC can collect one mid-flight.
_pending: set = set()


def schedule_group_icon_mirror(group_id, icon_url) -> None:
    """Mirror the icon in the background, off the request path.

    /group_search is served to every plugin version, including ones that have no
    use for the mirror, so it must not wait on a download. The icon appears on
    the next request instead of this one — the panel loads it asynchronously
    anyway. Never raises.
    """
    if not icon_url or img_relative(icon_url):
        return
    try:
        if os.path.exists(icon_mirror_path(group_id)):
            return
        task = asyncio.get_running_loop().create_task(
            ensure_group_icon(group_id, icon_url))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception:
        pass
