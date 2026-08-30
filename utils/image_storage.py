"""Backblaze B2 storage for the public image tree (models + user uploads).

Grew out of the disk-pressure work of 2026-08: the 3D model tree (25 GiB) and
the user-upload screenshot tree were the two structural disk hogs on a 410 GiB
box whose nightly backup needs 10 GiB free. Both now live in the existing
``droptracker-video`` B2 bucket behind Cloudflare, and are served from the same
CDN host the videos already use.

Key namespace — and why it is not negotiable
--------------------------------------------
Everything goes under ``dt_img/``. The application key this box holds is
**namePrefix-restricted to ``dt_``** (probed 2026-08-30): a PUT outside that
prefix does not fail with a clean 403 — B2's S3 layer slams the connection
(botocore surfaces ``ConnectionClosedError``/``BadStatusLine``), which looks
exactly like a network fault and cost real diagnosis time. Keep every key under
``dt_img/`` and the failure mode never comes back.

  dt_img/models/{player_id}/{fingerprint}[-pet].glb     character models
  dt_img/models/{player_id}/{fingerprint}.png           full-body renders
  dt_img/models/{player_id}/{fingerprint}-avatar.png    avatar crops
  dt_img/user-upload/{wom_id}/{type}/[{subfolder}/]...  submission screenshots

Serving: Cloudflare fronts the bucket at ``B2_CDN_BASE_URL`` (prod:
``https://video.droptracker.io``) and serves bucket keys 1:1 — the public URL
for a key is simply ``{base}/{key}``. ``B2_IMG_CDN_BASE_URL`` overrides the
base for images only, so images could later move to their own hostname without
touching the video pipeline.

Verification: B2's S3 API returns the object's MD5 as the ETag for single-part
uploads, so every ``put_*`` here checks the digest of what was sent against the
ETag that came back and raises on mismatch. That is what lets the model
migration delete local files afterwards — "uploaded" is proven, not assumed.

Existence checks ride a positive-only Redis cache: objects are immutable once
written (a fingerprint names its content; screenshots are write-once), so a
positive answer stays true until *we* delete the key — and ``delete_key``
drops the cache entry. Negative answers are never cached (the object may be
about to appear).

The sync functions block on network I/O; from async code use the ``a*``
wrappers, which run them in a thread.

Related: ``utils/b2_storage.py`` (the boto3 client + the video pipeline),
``services/player_model.py`` / ``gear_image.py`` / ``player_avatar.py`` (model
artifacts), ``utils/download.py`` (user uploads), ``scripts/
migrate_models_to_b2.py`` (the one-time move), ``scripts/prune_drop_images.py``
(retention, which deletes B2-hosted screenshots the same way it does local
ones).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Iterator, Optional

from utils.b2_storage import B2_BUCKET_NAME, _get_s3_client

IMG_PREFIX = "dt_img"
MODELS_PREFIX = "dt_img/models"
USER_UPLOAD_PREFIX = "dt_img/user-upload"

# Positive-only existence cache. A week is arbitrary but safe: entries are only
# ever written after a real HEAD/PUT proved the object exists, and deletes
# invalidate explicitly.
_EXISTS_CACHE_PREFIX = "b2img:exists:"
_EXISTS_CACHE_TTL = 7 * 24 * 3600

_CONTENT_TYPES = {
    ".glb": "model/gltf-binary",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class ImageStorageError(Exception):
    """An upload/verify failure. Callers that can fall back to local storage
    should catch this; callers that cannot should let it propagate."""


def offload_enabled() -> bool:
    """Whether new image writes go to B2 instead of the local tree.

    Env-gated (``IMG_B2_OFFLOAD``) rather than hardcoded so a dev box without
    B2 credentials keeps the original local-filesystem behaviour untouched.
    """
    return os.getenv("IMG_B2_OFFLOAD", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def cdn_base() -> str:
    """Public base URL images are served under (no trailing slash)."""
    base = os.getenv("B2_IMG_CDN_BASE_URL", "") or os.getenv("B2_CDN_BASE_URL", "")
    return base.rstrip("/")


def url_for(key: str) -> str:
    return f"{cdn_base()}/{key}"


def key_from_url(url: str) -> Optional[str]:
    """Bucket key for a CDN URL we issued, or None if it isn't one of ours.

    Only accepts keys under ``dt_img/`` — this is what lets retention code
    hand any stored ``image_url`` to this function and trust that a match is
    an object this module owns (never a video, never someone else's URL).
    """
    if not url:
        return None
    raw = url.strip()
    bases = {cdn_base(), os.getenv("B2_CDN_BASE_URL", "").rstrip("/"),
             "https://video.droptracker.io"}
    for base in bases:
        if base and raw.startswith(base + "/"):
            key = raw[len(base) + 1:].split("?", 1)[0].split("#", 1)[0]
            if key.startswith(IMG_PREFIX + "/") and ".." not in key:
                return key
            return None
    return None


def content_type_for(filename: str) -> str:
    _, ext = os.path.splitext(filename.lower())
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _redis():
    """Raw redis client, or None — the cache is an optimisation and every use
    below must survive Redis being absent (scripts, tests, outages)."""
    try:
        from utils.redis import RedisClient

        return RedisClient().client
    except Exception:
        return None


def _cache_exists(key: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.setex(_EXISTS_CACHE_PREFIX + key, _EXISTS_CACHE_TTL, "1")
    except Exception:
        pass


def _uncache_exists(key: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_EXISTS_CACHE_PREFIX + key)
    except Exception:
        pass


def _is_missing_error(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    status = getattr(exc, "response", {}).get(
        "ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def put_bytes(key: str, data: bytes, content_type: Optional[str] = None) -> str:
    """Upload and verify one object; returns its public URL.

    Raises ImageStorageError if the ETag B2 reports does not match the MD5 of
    what was sent — the object is deleted first, so a failed verify never
    leaves a corrupt object behind for a later exists-check to trust.
    """
    client = _get_s3_client()
    ctype = content_type or content_type_for(key)
    try:
        resp = client.put_object(
            Bucket=B2_BUCKET_NAME,
            Key=key,
            Body=data,
            ContentType=ctype,
        )
    except Exception as exc:
        raise ImageStorageError(f"put {key}: {type(exc).__name__}: {exc}") from exc

    etag = str(resp.get("ETag", "")).strip('"')
    # Single-part uploads answer with the payload MD5. A multipart ETag has a
    # "-partcount" suffix; nothing here uploads multipart, so treat it (or any
    # mismatch) as a failed write.
    if etag != hashlib.md5(data).hexdigest():
        try:
            client.delete_object(Bucket=B2_BUCKET_NAME, Key=key)
        except Exception:
            pass
        raise ImageStorageError(
            f"put {key}: ETag {etag!r} does not match payload md5")

    _cache_exists(key)
    return url_for(key)


def put_file(key: str, path: str, content_type: Optional[str] = None) -> str:
    """Upload a local file (read fully into memory — callers here deal in
    models capped at 8 MiB and screenshots far smaller)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return put_bytes(key, data, content_type or content_type_for(path))


def get_bytes(key: str) -> Optional[bytes]:
    """Object contents, or None if it does not exist."""
    client = _get_s3_client()
    try:
        obj = client.get_object(Bucket=B2_BUCKET_NAME, Key=key)
        return obj["Body"].read()
    except Exception as exc:
        if _is_missing_error(exc):
            return None
        raise ImageStorageError(f"get {key}: {type(exc).__name__}: {exc}") from exc


def head(key: str) -> Optional[dict]:
    """``{"size": int, "etag": str}`` for an object, or None if missing."""
    client = _get_s3_client()
    try:
        resp = client.head_object(Bucket=B2_BUCKET_NAME, Key=key)
    except Exception as exc:
        if _is_missing_error(exc):
            return None
        raise ImageStorageError(f"head {key}: {type(exc).__name__}: {exc}") from exc
    return {
        "size": int(resp.get("ContentLength", 0)),
        "etag": str(resp.get("ETag", "")).strip('"'),
    }


def key_exists(key: str, *, use_cache: bool = True) -> bool:
    if use_cache:
        r = _redis()
        if r is not None:
            try:
                if r.get(_EXISTS_CACHE_PREFIX + key):
                    return True
            except Exception:
                pass
    try:
        info = head(key)
    except ImageStorageError:
        # A transport error is not "missing", but the callers of this are
        # serving pages/notifications where "no image" degrades gracefully
        # and an exception would not.
        return False
    if info is None:
        return False
    _cache_exists(key)
    return True


def delete_key(key: str) -> bool:
    client = _get_s3_client()
    try:
        client.delete_object(Bucket=B2_BUCKET_NAME, Key=key)
    except Exception as exc:
        print(f"[image_storage] delete {key} failed: {exc}")
        return False
    _uncache_exists(key)
    return True


def list_keys(prefix: str) -> Iterator[dict]:
    """Yields ``{"key", "size", "etag", "last_modified"}`` for every object
    under ``prefix`` (last_modified is tz-aware UTC)."""
    client = _get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=B2_BUCKET_NAME, Prefix=prefix):
        for entry in page.get("Contents", []) or []:
            yield {
                "key": entry["Key"],
                "size": int(entry.get("Size", 0)),
                "etag": str(entry.get("ETag", "")).strip('"'),
                "last_modified": entry.get("LastModified"),
            }


# ── async wrappers ───────────────────────────────────────────────────────────

async def aput_bytes(key: str, data: bytes,
                     content_type: Optional[str] = None) -> str:
    return await asyncio.to_thread(put_bytes, key, data, content_type)


async def aput_file(key: str, path: str,
                    content_type: Optional[str] = None) -> str:
    return await asyncio.to_thread(put_file, key, path, content_type)


async def aget_bytes(key: str) -> Optional[bytes]:
    return await asyncio.to_thread(get_bytes, key)


async def akey_exists(key: str, *, use_cache: bool = True) -> bool:
    return await asyncio.to_thread(lambda: key_exists(key, use_cache=use_cache))


async def adelete_key(key: str) -> bool:
    return await asyncio.to_thread(delete_key, key)
