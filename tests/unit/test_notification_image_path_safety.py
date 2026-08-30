"""Image attachment resolution must not read arbitrary files or fetch anywhere.

A submission's ``image_url`` is attacker-influenced — the intake endpoint is
public — and the notification sender turns it back into either a local file to
upload or a URL to fetch. Before this was guarded, both halves were exploitable
from a single unauthenticated request:

* ``https://www.droptracker.io/img/../../../.env`` was string-replaced into
  ``/store/droptracker/disc/.env``, which exists, and uploaded into a Discord
  channel the requester controls — the bot token, DB password, Stripe key and
  JWT signing key in one message;
* any other http(s) value was fetched server-side, so ``http://127.0.0.1:31325``
  or the cloud metadata address turned the bot into an SSRF proxy with the
  response attached to an embed.

These tests pin both gates and, just as importantly, that ordinary hosted
screenshots and Discord CDN URLs still work — a gate that blocks everything
would silently strip every screenshot instead.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

for _name in ("services.contribution_notifications", "services.event_notifications"):
    sys.modules.setdefault(_name, MagicMock())

import importlib.util

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "notification_service.py",
)
_spec = importlib.util.spec_from_file_location("_notif_image_safety_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_notif_image_safety_under_test"] = ns
_spec.loader.exec_module(ns)

Service = ns.NotificationService


@pytest.fixture()
def img_root(tmp_path, monkeypatch):
    """A throwaway image tree, plus a secret file just outside it."""
    root = tmp_path / "img"
    (root / "itemdb").mkdir(parents=True)
    (root / "itemdb" / "4708.png").write_bytes(b"\x89PNG fake")
    (tmp_path / ".env").write_text("BOT_TOKEN=super-secret")
    monkeypatch.setattr(Service, "HOSTED_IMG_ROOT", str(root) + os.sep)
    return root, tmp_path


class TestHostedPathContainment:
    def test_a_real_hosted_image_resolves(self, img_root):
        got = Service.hosted_image_path("https://www.droptracker.io/img/itemdb/4708.png")
        assert got == str((img_root[0] / "itemdb" / "4708.png").resolve())

    def test_query_and_fragment_are_stripped(self, img_root):
        assert Service.hosted_image_path(
            "https://www.droptracker.io/img/itemdb/4708.png?v=2#x"
        ) is not None

    def test_traversal_to_the_dotenv_is_refused(self, img_root):
        # The exact string that used to exfiltrate production secrets.
        assert Service.hosted_image_path(
            "https://www.droptracker.io/img/../.env"
        ) is None

    def test_deep_traversal_is_refused(self, img_root):
        assert Service.hosted_image_path(
            "https://www.droptracker.io/img/user-upload/../../../../etc/passwd"
        ) is None

    def test_a_missing_file_inside_the_root_is_refused(self, img_root):
        assert Service.hosted_image_path(
            "https://www.droptracker.io/img/itemdb/does-not-exist.png"
        ) is None

    def test_a_foreign_host_is_not_treated_as_hosted(self, img_root):
        assert Service.hosted_image_path("https://evil.example.com/img/x.png") is None

    def test_empty_and_non_string_are_refused(self, img_root):
        for value in (None, "", 42, b"bytes"):
            assert Service.hosted_image_path(value) is None


class TestRemoteFetchAllowlist:
    def test_discord_cdn_is_allowed(self):
        assert Service._remote_image_allowed(
            "https://cdn.discordapp.com/attachments/1/2/shot.png") is True

    def test_discord_media_subdomain_is_allowed(self):
        assert Service._remote_image_allowed(
            "https://media.discordapp.net/attachments/1/2/shot.png") is True

    def test_loopback_is_refused(self):
        # Our own web API listens here; fetching it would proxy internal data.
        assert Service._remote_image_allowed("http://127.0.0.1:31325/api/v1/me") is False

    def test_cloud_metadata_is_refused(self):
        assert Service._remote_image_allowed(
            "http://169.254.169.254/latest/meta-data/") is False

    def test_an_arbitrary_host_is_refused(self):
        assert Service._remote_image_allowed("https://evil.example.com/x.png") is False

    def test_a_lookalike_host_is_refused(self):
        # Suffix matching must be on a dot boundary, not a substring.
        assert Service._remote_image_allowed(
            "https://cdn.discordapp.com.evil.example/x.png") is False

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "gopher://x/1", "ftp://cdn.discordapp.com/x"):
            assert Service._remote_image_allowed(url) is False


class TestResolveImageAttachment:
    """The resolver’s three-source order: local file, our B2 bucket, then the
    allowlisted remote fetch.

    Pinned after the 2026-08-30 B2 offload silently stripped every screenshot:
    the old hosted branch claimed anything containing "droptracker.io" — which
    matched the new video.droptracker.io CDN URLs — mapped them to a local
    path that no longer exists, and returned before the fallback could run.
    """

    @staticmethod
    def _service():
        svc = Service.__new__(Service)  # resolver needs no __init__ state
        return svc

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)

    @pytest.fixture(autouse=True)
    def _cdn_env(self, monkeypatch):
        monkeypatch.setenv("B2_CDN_BASE_URL", "https://video.droptracker.io")

        # `interactions` is conftest-stubbed; stand in a File with the real
        # class's naming behaviour (path basename / "file" for raw IO) so the
        # file_name assertions test what Discord would actually see.
        class _FakeFile:
            def __init__(self, file, file_name=None, **_kw):
                self.file = file
                if file_name is not None:
                    self.file_name = file_name
                elif hasattr(file, "read"):
                    self.file_name = "file"
                else:
                    self.file_name = os.path.basename(str(file))

        monkeypatch.setattr(ns.interactions, "File", _FakeFile)

    def test_local_file_still_wins(self, img_root):
        att, tmp = self._run(self._service()._resolve_image_attachment(
            "https://www.droptracker.io/img/itemdb/4708.png", 1))
        assert att is not None and tmp is None
        assert str(att.file).endswith("itemdb/4708.png")

    def test_b2_url_attaches_object_bytes_from_memory(self, monkeypatch):
        from utils import image_storage

        async def fake_get(key):
            assert key == "dt_img/user-upload/1/level_up/Farming/lvl_ab12cd34.png"
            return b"png-bytes"

        monkeypatch.setattr(image_storage, "aget_bytes", fake_get)
        att, tmp = self._run(self._service()._resolve_image_attachment(
            "https://video.droptracker.io/dt_img/user-upload/1/level_up/"
            "Farming/lvl_ab12cd34.png", 1))
        assert att is not None and tmp is None
        # A bare IOBase upload is named "file" (no extension) by interactions,
        # which Discord will not render as an image — and the components path
        # references attachment://{file_name}. The basename must survive.
        assert att.file_name == "lvl_ab12cd34.png"
        assert att.file.read() == b"png-bytes"

    def test_missing_b2_object_degrades_to_no_image(self, monkeypatch):
        from utils import image_storage

        async def fake_get(key):
            return None

        monkeypatch.setattr(image_storage, "aget_bytes", fake_get)
        att, tmp = self._run(self._service()._resolve_image_attachment(
            "https://video.droptracker.io/dt_img/user-upload/1/drop/x.png", 1))
        assert (att, tmp) == (None, None)

    def test_b2_read_never_goes_through_http(self, monkeypatch):
        """The bucket read is server-side; aiohttp must not be touched."""
        from utils import image_storage

        async def fake_get(key):
            return b"d"

        monkeypatch.setattr(image_storage, "aget_bytes", fake_get)
        monkeypatch.setattr(
            ns.aiohttp, "ClientSession",
            lambda *a, **k: pytest.fail("B2 URLs must not be fetched over HTTP"))
        att, _ = self._run(self._service()._resolve_image_attachment(
            "https://video.droptracker.io/dt_img/user-upload/1/drop/x.png", 1))
        assert att is not None

    def test_dangling_hosted_url_does_not_fetch(self, img_root, monkeypatch):
        """An unmapped www URL answers (None, None) without a network trip —
        www.droptracker.io is deliberately not on the remote allowlist."""
        monkeypatch.setattr(
            ns.aiohttp, "ClientSession",
            lambda *a, **k: pytest.fail("hosted URLs must never be fetched"))
        att, tmp = self._run(self._service()._resolve_image_attachment(
            "https://www.droptracker.io/img/user-upload/9/drop/gone.jpg", 1))
        assert (att, tmp) == (None, None)

    def test_ssrf_targets_still_refused(self, monkeypatch):
        monkeypatch.setattr(
            ns.aiohttp, "ClientSession",
            lambda *a, **k: pytest.fail("disallowed hosts must never be fetched"))
        for url in ("http://127.0.0.1:31325/api/v1/me",
                    "http://169.254.169.254/latest/meta-data/",
                    "https://evil.example.com/x.png"):
            att, tmp = self._run(
                self._service()._resolve_image_attachment(url, 1))
            assert (att, tmp) == (None, None)

    def test_videos_on_our_cdn_are_not_image_keys(self):
        # Same host, different namespace: key_from_url only owns dt_img/.
        assert Service._b2_image_key(
            "https://video.droptracker.io/videos/123/clip.mp4") is None
