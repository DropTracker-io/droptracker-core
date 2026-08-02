"""Unit tests for the plugin-facing "paths, not URLs" contract.

The RuneLite plugin may only contact hosts that are hardcoded in the plugin
(Plugin Hub rule: fetching a URL that arrived in an API response is an SSRF risk
and makes the plugin's domain set unreviewable). So every image the plugin
renders and every webhook it posts to is described to it as a *path* or a *pair
of credentials*, and the plugin supplies the host.

These tests pin the producing side of that contract. If one of them starts
returning a full URL again, the plugin silently stops rendering that asset.
"""

from unittest.mock import AsyncMock, patch

import pytest

from utils.group_icon import icon_relative_path
from utils.plugin_urls import discord_invite_code, webhook_credentials


# ── webhook credentials ───────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://discord.com/api/webhooks/123456789012345678/abcDEF-_012345678901",
     "123456789012345678/abcDEF-_012345678901"),
    # Legacy rows may hold the discordapp.com host; the host is discarded either way.
    ("https://discordapp.com/api/webhooks/123456789012345678/tok.en-123",
     "123456789012345678/tok.en-123"),
    # Already-bare credentials pass through unchanged.
    ("123456789012345678/token1234567890", "123456789012345678/token1234567890"),
    # Query strings and trailing slashes are noise, not part of the credential.
    ("https://discord.com/api/webhooks/1234567890/tok?wait=true", "1234567890/tok"),
    ("https://discord.com/api/webhooks/1234567890/tok/", "1234567890/tok"),
])
def test_webhook_credentials_extracts_id_and_token(url, expected):
    assert webhook_credentials(url) == expected


@pytest.mark.parametrize("url", [
    None,
    "",
    "   ",
    "https://evil.example/x",             # not a webhook at all
    "https://discord.com/api/webhooks/",  # no id or token
    "https://discord.com/api/webhooks/notanid/token",
    "https://discord.com/api/webhooks/123/tok/extra",
])
def test_webhook_credentials_rejects_non_webhooks(url):
    assert webhook_credentials(url) is None


def test_webhook_credentials_never_leaks_a_host():
    # Whatever host the stored row claims, the published credential carries none.
    result = webhook_credentials("https://evil.example/api/webhooks/1234567890/tokenvalue")
    assert result == "1234567890/tokenvalue"
    assert "evil.example" not in result
    assert "://" not in result


# ── discord invite codes ──────────────────────────────────────────────────────

@pytest.mark.parametrize("invite,expected", [
    ("https://discord.gg/droptracker", "droptracker"),
    ("https://discord.com/invite/abc-123", "abc-123"),
    ("https://discordapp.com/invite/xyz", "xyz"),
    ("discord.gg/xyz/", "xyz"),
    ("droptracker", "droptracker"),          # already a bare code
    ("https://discord.gg/abc?event=1", "abc"),
])
def test_discord_invite_code_extracts_code(invite, expected):
    assert discord_invite_code(invite) == expected


@pytest.mark.parametrize("invite", [
    None,
    "",
    "https://evil.example/steal",   # a non-Discord link must not become a code
    "https://discord.gg/",
    "a/b",
])
def test_discord_invite_code_rejects_non_invites(invite):
    assert discord_invite_code(invite) is None


# ── group icon paths ──────────────────────────────────────────────────────────

def test_icon_relative_path_strips_our_own_host():
    assert icon_relative_path(2, "https://www.droptracker.io/img/clans/2/icon.png") \
        == "clans/2/icon.png"
    assert icon_relative_path(2, "https://droptracker.io/img/clans/2/icon.png") \
        == "clans/2/icon.png"


def test_icon_relative_path_returns_none_for_unmirrored_remote_icon():
    # A Discord CDN icon has no path until ensure_group_icon() has copied it
    # locally; the plugin then renders no icon, as it does for a group with none.
    with patch("os.path.exists", return_value=False):
        assert icon_relative_path(2, "https://cdn.discordapp.com/icons/1/a.png") is None


def test_icon_relative_path_uses_mirror_once_present():
    with patch("os.path.exists", return_value=True):
        assert icon_relative_path(7, "https://cdn.discordapp.com/icons/1/a.png") \
            == "clans/7/icon.png"


@pytest.mark.parametrize("icon_url", [None, "", "   "])
def test_icon_relative_path_handles_missing_icon(icon_url):
    assert icon_relative_path(2, icon_url) is None


# ── submission image paths ────────────────────────────────────────────────────

def test_img_path_relativises_stored_screenshot_paths():
    from api.routes.helpers import _img_path

    # Current rows store the public URL; older rows store the on-disk path.
    # Both must resolve, or recent submissions render no screenshot at all.
    assert _img_path("https://www.droptracker.io/img/user-upload/520173/drop/Phantom_Muspah/Fire_rune_0_6.jpg") \
        == "user-upload/520173/drop/Phantom_Muspah/Fire_rune_0_6.jpg"
    # Real paths carry apostrophes — these must survive, not be dropped.
    assert _img_path("https://www.droptracker.io/img/user-upload/1/drop/Phosani's_Nightmare/a.jpg") \
        == "user-upload/1/drop/Phosani's_Nightmare/a.jpg"
    assert _img_path("/store/droptracker/disc/static/assets/img/user-upload/1/drop/x.jpg") \
        == "user-upload/1/drop/x.jpg"
    assert _img_path("static/assets/img/user-upload/1/drop/x.jpg") \
        == "user-upload/1/drop/x.jpg"
    # Anything stored outside the served image root has no public path.
    assert _img_path("/etc/passwd") is None
    assert _img_path(None) is None
    assert _img_path("") is None


# ── icon mirroring is not retried on every request ────────────────────────────

def test_failed_icon_mirror_is_not_retried_immediately():
    """A group whose icon URL 404s must not pay a download timeout per request.

    ensure_group_icon runs on every /group_search, so an unremembered failure
    would re-attempt the fetch forever.
    """
    import asyncio

    from utils import group_icon

    group_icon._recent_failures.clear()
    url = "https://cdn.discordapp.com/icons/1/missing.png"

    class _Response:
        status = 404

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read(self):
            return b""

    class _Session:
        calls = 0

        def get(self, *args, **kwargs):
            _Session.calls += 1
            return _Response()

        async def close(self):
            return None

    session = _Session()
    with patch("os.path.exists", return_value=False):
        assert asyncio.run(group_icon.ensure_group_icon(1, url, session=session)) is False
        assert asyncio.run(group_icon.ensure_group_icon(1, url, session=session)) is False
    assert _Session.calls == 1
    group_icon._recent_failures.clear()


# ── old clients must see exactly what they saw before ─────────────────────────

def test_icon_url_is_unchanged_for_existing_clients():
    """/event_state still emits the same absolute icon_url it always did.

    The internal representation moved to relative paths so the new plugin can be
    handed icon_path, but plugin versions in the wild read icon_url. If _img_url
    ever stops reproducing the old "{IMG_BASE}/{rel}" exactly, every icon in
    every deployed client breaks.
    """
    # Loaded from the file path: the conftest stubs the ``services`` package.
    # Same approach as test_plugin_notifications.py.
    import importlib.util
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "services", "plugin_notifications.py")
    spec = importlib.util.spec_from_file_location("services.plugin_notifications", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["services.plugin_notifications"] = module
    spec.loader.exec_module(module)
    IMG_BASE, _img_url = module.IMG_BASE, module._img_url

    assert _img_url("metrics/magic.png") == f"{IMG_BASE}/metrics/magic.png"
    assert _img_url("npcdb/2215.png") == "https://www.droptracker.io/img/npcdb/2215.png"
    # A task with no resolvable icon emitted None before and must still do so.
    assert _img_url(None) is None
    assert _img_url("") is None


def test_group_icon_mirror_never_blocks_the_request():
    """schedule_group_icon_mirror must not perform the download inline.

    /group_search is served to every plugin version; an awaited fetch would add
    the download's latency to clients that have no use for the mirror.
    """
    import inspect

    from utils.group_icon import schedule_group_icon_mirror

    assert not inspect.iscoroutinefunction(schedule_group_icon_mirror)
    # Outside an event loop it is a no-op rather than an error.
    assert schedule_group_icon_mirror(1, "https://cdn.discordapp.com/icons/1/a.png") is None
    # An already-local icon needs no mirror at all.
    assert schedule_group_icon_mirror(1, "https://www.droptracker.io/img/clans/1/icon.png") is None
