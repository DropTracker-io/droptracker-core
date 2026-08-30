"""B2-mode behaviour of the model services (player_model / player_avatar).

The local-mode tests live in test_player_model.py / test_player_avatar.py and
still pass untouched — that IS the dev-box contract. These cover the branch
prod runs: keys mirror the on-disk layout, the prune keeps the same keep*2 /
protect / renders-survive semantics against a bucket listing, and the avatar
derive-and-cache works from bucket bytes instead of paths.

``utils.image_storage`` is real (its boto3 layer is conftest-stubbed); its
IO functions are monkeypatched per test.
"""
from __future__ import annotations

import io
import struct
import sys
from datetime import datetime, timezone

import pytest

from services.player_model import _fingerprint_of_filename  # noqa: F401 (real module, conftest-loaded)
from utils import image_storage

# `from services import player_model` would answer the stubbed package's
# MagicMock attribute; the real module lives in sys.modules under its dotted
# name (conftest loads it by file path).
player_model = sys.modules["services.player_model"]
player_avatar = sys.modules["services.player_avatar"]


@pytest.fixture(autouse=True)
def _b2_mode(monkeypatch):
    monkeypatch.setenv("IMG_B2_OFFLOAD", "true")
    monkeypatch.setenv("B2_CDN_BASE_URL", "https://video.droptracker.io")
    monkeypatch.delenv("B2_IMG_CDN_BASE_URL", raising=False)


def _glb() -> bytes:
    payload = b'{"asset":{"version":"2.0"}}' + b" "
    body = struct.pack("<II", len(payload), 0x4E4F534A) + payload
    return struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body


class TestModelStorage:
    def test_urls_and_keys_mirror_the_local_layout(self):
        assert player_model.model_key(7, "abcd") == "dt_img/models/7/abcd.glb"
        assert player_model.model_key(7, "abcd", pet=True) == \
            "dt_img/models/7/abcd-pet.glb"
        assert player_model.model_url(7, "abcd") == \
            "https://video.droptracker.io/dt_img/models/7/abcd.glb"

    def test_store_uploads_validated_model(self, monkeypatch):
        calls = []

        def fake_put(key, data, content_type=None):
            calls.append((key, len(data), content_type))
            return image_storage.url_for(key)

        monkeypatch.setattr(image_storage, "put_bytes", fake_put)
        url = player_model.store_model(7, "abcd", _glb())
        assert url == "https://video.droptracker.io/dt_img/models/7/abcd.glb"
        assert calls == [("dt_img/models/7/abcd.glb", len(_glb()),
                          "model/gltf-binary")]

    def test_store_still_validates_before_upload(self, monkeypatch):
        calls = []
        monkeypatch.setattr(image_storage, "put_bytes",
                            lambda *a, **k: calls.append(a))
        assert player_model.store_model(7, "abcd", b"not a glb") is None
        assert player_model.store_model(7, "../evil", _glb()) is None
        assert calls == []

    def test_store_fails_plainly_on_b2_error(self, monkeypatch):
        def boom(*a, **k):
            raise image_storage.ImageStorageError("down")

        monkeypatch.setattr(image_storage, "put_bytes", boom)
        assert player_model.store_model(7, "abcd", _glb()) is None

    def test_exists_uses_cached_head(self, monkeypatch):
        seen = []
        monkeypatch.setattr(image_storage, "key_exists",
                            lambda key, **k: seen.append(key) or True)
        assert player_model.model_exists(7, "abcd", pet=True)
        assert seen == ["dt_img/models/7/abcd-pet.glb"]
        # An invalid fingerprint never reaches the bucket.
        assert not player_model.model_exists(7, "ZZ")
        assert len(seen) == 1


class TestPruneB2:
    def _listing(self, entries):
        base = datetime(2026, 8, 1, tzinfo=timezone.utc)
        return [
            {"key": f"dt_img/models/7/{name}", "size": 1,
             "etag": "e", "last_modified": base.replace(day=day)}
            for name, day in entries
        ]

    def test_prune_keeps_window_protects_pin_and_spares_renders(self, monkeypatch):
        # 3 outfits, keep=1 (window = 2 glb files). "oldest" is pinned.
        listing = self._listing([
            ("newest.glb", 28), ("newest-avatar.png", 28), ("newest.png", 28),
            ("middle.glb", 15), ("middle-pet.glb", 15), ("middle.png", 15),
            ("oldest.glb", 1), ("oldest.png", 1),
        ])
        deleted = []
        monkeypatch.setattr(image_storage, "list_keys",
                            lambda prefix: iter(listing))
        monkeypatch.setattr(image_storage, "delete_key",
                            lambda key: deleted.append(key) or True)

        removed = player_model.prune_old_models(
            7, keep=1, protect=frozenset({"oldest"}))

        # newest.glb + middle.glb fill the keep*2 window; middle-pet.glb is
        # third and goes (with its avatar crop); pinned "oldest" survives.
        assert removed == 1
        assert "dt_img/models/7/middle-pet.glb" in deleted
        assert "dt_img/models/7/middle-avatar.png" in deleted
        assert not any(k.endswith("oldest.glb") for k in deleted)
        # Renders are never pruned: old Discord embeds point at them forever.
        assert not any(k.endswith("middle.png") for k in deleted)

    def test_listing_failure_is_contained(self, monkeypatch):
        def boom(prefix):
            raise image_storage.ImageStorageError("listing failed")

        monkeypatch.setattr(image_storage, "list_keys", boom)
        assert player_model.prune_old_models(7) == 0


class TestAvatarB2:
    def _png_render(self) -> bytes:
        # A figure the crop geometry accepts: tall opaque block resting near
        # the bottom of the frame (see player_avatar's sanity checks).
        from PIL import Image

        img = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
        for x in range(180, 220):
            for y in range(200, 580):
                img.putpixel((x, y), (255, 255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def test_hit_answers_url_without_deriving(self, monkeypatch):


        monkeypatch.setattr(image_storage, "key_exists", lambda key, **k: True)
        monkeypatch.setattr(image_storage, "get_bytes",
                            lambda key: pytest.fail("must not fetch on a hit"))
        url = player_avatar.ensure_avatar(7, "abcd")
        assert url == \
            "https://video.droptracker.io/dt_img/models/7/abcd-avatar.png"

    def test_derives_from_render_bytes_and_stores(self, monkeypatch):


        stored = {}
        monkeypatch.setattr(image_storage, "key_exists", lambda key, **k: False)
        monkeypatch.setattr(image_storage, "get_bytes",
                            lambda key: self._png_render()
                            if key == "dt_img/models/7/abcd.png" else None)

        def fake_put(key, data, content_type=None):
            stored[key] = (data, content_type)
            return image_storage.url_for(key)

        monkeypatch.setattr(image_storage, "put_bytes", fake_put)

        url = player_avatar.ensure_avatar(7, "abcd")
        assert url.endswith("/dt_img/models/7/abcd-avatar.png")
        data, ctype = stored["dt_img/models/7/abcd-avatar.png"]
        assert ctype == "image/png"
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_no_render_answers_none(self, monkeypatch):


        monkeypatch.setattr(image_storage, "key_exists", lambda key, **k: False)
        monkeypatch.setattr(image_storage, "get_bytes", lambda key: None)
        assert player_avatar.ensure_avatar(7, "abcd") is None


class TestFingerprintOfKeyNames:
    def test_pet_suffix_folds_to_the_outfit(self):
        assert _fingerprint_of_filename("abcd-pet.glb") == "abcd"
        assert _fingerprint_of_filename("abcd.glb") == "abcd"
