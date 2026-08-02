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
