"""B2 offload of new user-upload screenshots (utils/download.py).

``utils.download`` is conftest-stubbed (its module imports pull in aiohttp and
the DB layer), so the real module is loaded by file path here, pointed at a
temp tree. What must hold: B2-mode filenames are unique by construction (the
local collision scan can't see bucket objects), the key mirrors the layout
under ``dt_img/user-upload/``, callers get the CDN URL, nothing lands in the
local tree on success — and a B2 failure degrades to exactly the pre-offload
local write.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def download(tmp_path, monkeypatch):
    monkeypatch.setenv("IMG_B2_OFFLOAD", "true")
    monkeypatch.setenv("B2_CDN_BASE_URL", "https://video.droptracker.io")
    spec = importlib.util.spec_from_file_location(
        "_download_under_test", REPO_ROOT / "utils" / "download.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "USER_UPLOAD_ROOT", str(tmp_path) + os.sep)
    monkeypatch.setattr(module, "USER_UPLOAD_BASE_URL",
                        "https://www.droptracker.io/img/user-upload/")
    try:
        yield module, tmp_path
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture
def b2(monkeypatch):
    """Recorded fakes over the real image_storage module."""
    from utils import image_storage

    state = SimpleNamespace(puts=[], fail=False)

    async def fake_aput_bytes(key, data, content_type=None):
        if state.fail:
            raise image_storage.ImageStorageError("bucket down")
        state.puts.append((key, bytes(data), content_type))
        return image_storage.url_for(key)

    async def fake_aput_file(key, path, content_type=None):
        with open(path, "rb") as fh:
            return await fake_aput_bytes(key, fh.read(), content_type)

    monkeypatch.setattr(image_storage, "aput_bytes", fake_aput_bytes)
    monkeypatch.setattr(image_storage, "aput_file", fake_aput_file)
    return state


class _FakeResponse:
    def __init__(self, status=200, body=b"jpeg-bytes"):
        self.status = status
        self._body = body
        self.content = self

    async def read(self, *_):
        # download_player_image's B2 branch reads the whole body at once;
        # the streaming fallback path is not exercised through this fake.
        body, self._body = self._body, b""
        return body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClientSession:
    def __init__(self, response):
        self._response = response

    def get(self, url):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_http(module, monkeypatch, response):
    fake = SimpleNamespace(
        ClientSession=lambda: _FakeClientSession(response),
        ClientError=Exception,
    )
    monkeypatch.setattr(module, "aiohttp", fake)


PLAYER = SimpleNamespace(wom_id=4242, player_name="Tester")


class TestDownloadPlayerImage:
    def test_offloads_to_b2_and_returns_cdn_url(self, download, b2, monkeypatch):
        module, tmp = download
        _patch_http(module, monkeypatch, _FakeResponse())

        path, url = asyncio.run(module.download_player_image(
            submission_type="pb", file_name="ignored", player=PLAYER,
            attachment_url="https://cdn.discordapp.com/x.jpg",
            file_extension="jpg", entry_id=17, entry_name="Zulrah",
            npc_name="Zulrah"))

        assert path is not None            # callers null-check this
        (key, data, ctype), = b2.puts
        assert re.fullmatch(
            r"dt_img/user-upload/4242/pb/Zulrah/Zulrah_17_[0-9a-f]{8}\.jpg",
            key)
        assert data == b"jpeg-bytes"
        assert ctype == "image/jpeg"
        assert url == f"https://video.droptracker.io/{key}"
        # Nothing may land in (or even create) the local tree.
        assert list(tmp.iterdir()) == []

    def test_names_cannot_collide(self, download, b2, monkeypatch):
        module, _ = download
        urls = set()
        for _ in range(2):
            _patch_http(module, monkeypatch, _FakeResponse())
            _, url = asyncio.run(module.download_player_image(
                "pb", "f", PLAYER, "https://cdn/x.jpg", "jpg", 17, "Zulrah"))
            urls.add(url)
        # Same entry submitted twice must produce two distinct objects — the
        # old directory scan can't defend uniqueness in a bucket.
        assert len(urls) == 2

    def test_b2_failure_falls_back_to_local_write(self, download, b2,
                                                  monkeypatch):
        module, tmp = download
        b2.fail = True
        _patch_http(module, monkeypatch, _FakeResponse())

        path, url = asyncio.run(module.download_player_image(
            "pb", "f", PLAYER, "https://cdn/x.jpg", "jpg", 17, "Zulrah",
            npc_name="Zulrah"))

        assert Path(path).read_bytes() == b"jpeg-bytes"
        assert url.startswith(
            "https://www.droptracker.io/img/user-upload/4242/pb/Zulrah/")

    def test_http_error_still_answers_none_none(self, download, b2,
                                                monkeypatch):
        module, _ = download
        _patch_http(module, monkeypatch, _FakeResponse(status=404))
        path, url = asyncio.run(module.download_player_image(
            "pb", "f", PLAYER, "https://cdn/x.jpg", "jpg", 17, "Zulrah"))
        assert (path, url) == (None, None)
        assert b2.puts == []


class _SaveUpload:
    """A FileStorage-style upload object (sync .save)."""

    filename = "shot.png"
    content_type = "image/png"

    def __init__(self, body=b"png-bytes"):
        self._body = body

    def save(self, path):
        with open(path, "wb") as fh:
            fh.write(self._body)


class TestDownloadImage:
    def test_offloads_and_reports_cdn_url_as_image_path(self, download, b2):
        module, tmp = download
        processed = {"npc_name": "Zulrah", "item": "Tanzanite fang"}

        result = asyncio.run(module.download_image(
            "drop", PLAYER, 4242, _SaveUpload(), processed))

        (key, data, _), = b2.puts
        assert re.fullmatch(
            r"dt_img/user-upload/4242/drop/Zulrah/"
            r"Zulrah_Tanzanite_fang_[0-9a-f]{8}\.png", key)
        assert data == b"png-bytes"
        assert processed["image_path"] == f"https://video.droptracker.io/{key}"
        assert result == processed["image_path"]
        assert list(tmp.iterdir()) == []
        # The temp staging file is gone too.
        assert not [p for p in Path(module.tempfile.gettempdir()).glob(
            "*_Zulrah_Tanzanite_fang_*")]

    def test_b2_failure_falls_back_to_local_tree(self, download, b2):
        module, tmp = download
        b2.fail = True
        processed = {"npc_name": "Zulrah", "item": "Tanzanite fang"}

        result = asyncio.run(module.download_image(
            "drop", PLAYER, 4242, _SaveUpload(), processed))

        assert result is not None
        saved = Path(result)
        assert saved.exists() and saved.read_bytes() == b"png-bytes"
        assert str(saved).startswith(str(tmp))
        assert processed["image_path"].startswith(
            "https://www.droptracker.io/img/user-upload/4242/drop/Zulrah/")
