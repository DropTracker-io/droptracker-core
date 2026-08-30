"""Unit tests for utils/image_storage — the B2 image tree.

The interesting properties are the ones that make the offload *safe*: URLs
only ever map back to keys we own (retention deletes ride on that), uploads
are verified against the ETag md5 before anyone trusts them, and the
existence cache can never hold a positive answer for an object that was
deleted through us.

boto3 never runs here: ``utils.b2_storage`` is conftest-stubbed, and the
client is replaced per test.
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from utils import image_storage


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("B2_CDN_BASE_URL", "https://video.droptracker.io")
    monkeypatch.delenv("B2_IMG_CDN_BASE_URL", raising=False)
    monkeypatch.delenv("IMG_B2_OFFLOAD", raising=False)


class FakeRedis:
    def __init__(self):
        self.store = {}

    def setex(self, key, ttl, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(image_storage, "_redis", lambda: fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(image_storage, "_get_s3_client", lambda: fake)
    return fake


class TestUrls:
    def test_roundtrip(self):
        key = "dt_img/models/123/abcd.glb"
        url = image_storage.url_for(key)
        assert url == "https://video.droptracker.io/dt_img/models/123/abcd.glb"
        assert image_storage.key_from_url(url) == key

    def test_query_and_fragment_are_stripped(self):
        url = "https://video.droptracker.io/dt_img/user-upload/1/drop/x.jpg?w=1#f"
        assert image_storage.key_from_url(url) == "dt_img/user-upload/1/drop/x.jpg"

    def test_foreign_hosts_and_namespaces_are_rejected(self):
        # A URL we did not issue must never become a deletable key.
        assert image_storage.key_from_url(
            "https://www.droptracker.io/img/user-upload/1/drop/x.jpg") is None
        assert image_storage.key_from_url(
            "https://video.droptracker.io/videos/123/clip.mp4") is None
        assert image_storage.key_from_url(
            "https://video.droptracker.io/dt_img/../videos/clip.mp4") is None
        assert image_storage.key_from_url("") is None
        assert image_storage.key_from_url(None) is None

    def test_img_base_override_wins(self, monkeypatch):
        monkeypatch.setenv("B2_IMG_CDN_BASE_URL", "https://img.example.com")
        assert image_storage.url_for("dt_img/x.png") == \
            "https://img.example.com/dt_img/x.png"
        # ...and both bases keep resolving on the way back in.
        assert image_storage.key_from_url(
            "https://img.example.com/dt_img/x.png") == "dt_img/x.png"
        assert image_storage.key_from_url(
            "https://video.droptracker.io/dt_img/x.png") == "dt_img/x.png"


class TestBasics:
    def test_offload_flag_parsing(self, monkeypatch):
        assert not image_storage.offload_enabled()
        for value in ("1", "true", "YES", " on "):
            monkeypatch.setenv("IMG_B2_OFFLOAD", value)
            assert image_storage.offload_enabled()
        monkeypatch.setenv("IMG_B2_OFFLOAD", "false")
        assert not image_storage.offload_enabled()

    def test_content_types(self):
        assert image_storage.content_type_for("a/b/c.glb") == "model/gltf-binary"
        assert image_storage.content_type_for("x.PNG") == "image/png"
        assert image_storage.content_type_for("x.jpeg") == "image/jpeg"
        assert image_storage.content_type_for("weird.bin") == \
            "application/octet-stream"


class TestPutBytes:
    def test_verified_upload_returns_url_and_caches(self, client, redis):
        data = b"model-bytes"
        client.put_object.return_value = {
            "ETag": f'"{hashlib.md5(data).hexdigest()}"'}

        url = image_storage.put_bytes("dt_img/models/1/ab.glb", data)

        assert url.endswith("/dt_img/models/1/ab.glb")
        kwargs = client.put_object.call_args.kwargs
        assert kwargs["Key"] == "dt_img/models/1/ab.glb"
        assert kwargs["ContentType"] == "model/gltf-binary"
        assert "b2img:exists:dt_img/models/1/ab.glb" in redis.store

    def test_etag_mismatch_raises_and_deletes(self, client, redis):
        client.put_object.return_value = {"ETag": '"not-the-md5"'}

        with pytest.raises(image_storage.ImageStorageError):
            image_storage.put_bytes("dt_img/models/1/ab.glb", b"data")

        # The corrupt object must not survive to be trusted by a later
        # exists-check, and nothing may be cached for it.
        client.delete_object.assert_called_once()
        assert redis.store == {}

    def test_transport_error_raises(self, client, redis):
        client.put_object.side_effect = ConnectionError("slammed")
        with pytest.raises(image_storage.ImageStorageError):
            image_storage.put_bytes("dt_img/x.png", b"d")


class TestExistsCache:
    def test_cache_hit_skips_the_head(self, client, redis):
        redis.store["b2img:exists:dt_img/a.png"] = "1"
        assert image_storage.key_exists("dt_img/a.png")
        client.head_object.assert_not_called()

    def test_miss_heads_and_caches_positive(self, client, redis):
        client.head_object.return_value = {"ContentLength": 5, "ETag": '"x"'}
        assert image_storage.key_exists("dt_img/a.png")
        assert "b2img:exists:dt_img/a.png" in redis.store

    def test_negative_answers_are_never_cached(self, client, redis):
        exc = Exception("nope")
        exc.response = {"Error": {"Code": "404"},
                        "ResponseMetadata": {"HTTPStatusCode": 404}}
        client.head_object.side_effect = exc
        assert not image_storage.key_exists("dt_img/a.png")
        assert redis.store == {}

    def test_delete_invalidates(self, client, redis):
        redis.store["b2img:exists:dt_img/a.png"] = "1"
        assert image_storage.delete_key("dt_img/a.png")
        assert redis.store == {}

    def test_survives_redis_absence(self, client, monkeypatch):
        monkeypatch.setattr(image_storage, "_redis", lambda: None)
        client.head_object.return_value = {"ContentLength": 5, "ETag": '"x"'}
        assert image_storage.key_exists("dt_img/a.png")


class TestGetAndHead:
    def test_get_missing_returns_none(self, client):
        exc = Exception("missing")
        exc.response = {"Error": {"Code": "NoSuchKey"},
                        "ResponseMetadata": {"HTTPStatusCode": 404}}
        client.get_object.side_effect = exc
        assert image_storage.get_bytes("dt_img/a.png") is None

    def test_head_shape(self, client):
        client.head_object.return_value = {
            "ContentLength": 42, "ETag": '"abc"'}
        assert image_storage.head("dt_img/a.png") == {"size": 42, "etag": "abc"}

    def test_list_keys_shape(self, client):
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [
                {"Key": "dt_img/models/1/a.glb", "Size": 7,
                 "ETag": '"e"', "LastModified": "LM"},
            ]},
            {},  # a page with no Contents must not blow up
        ]
        items = list(image_storage.list_keys("dt_img/models/"))
        assert items == [{"key": "dt_img/models/1/a.glb", "size": 7,
                          "etag": "e", "last_modified": "LM"}]
