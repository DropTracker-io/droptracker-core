"""
Storage abstraction for the video pipeline.

Keeps existing B2 behavior as default while allowing a local-filesystem backend
for testing and future cutover.
"""

import asyncio
import os
import shutil
from pathlib import Path

from utils.b2_storage import (
    get_public_video_url as b2_get_public_video_url,
    download_object as b2_download_object,
    upload_object as b2_upload_object,
    delete_object as b2_delete_object,
    object_exists as b2_object_exists,
)


def _env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    """Read an integer env var with optional clamping."""
    raw = os.getenv(name, str(default))
    try:
        value = int(str(raw).strip())
    except Exception:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


VIDEO_STORAGE_BACKEND_DEFAULT = os.getenv("VIDEO_STORAGE_BACKEND_DEFAULT", "b2").strip().lower()
if VIDEO_STORAGE_BACKEND_DEFAULT not in {"b2", "local"}:
    VIDEO_STORAGE_BACKEND_DEFAULT = "b2"

# Local video storage paths. For disk I/O tuning, point VIDEO_LOCAL_ROOT to a
# separate fast disk (e.g. SSD or dedicated volume) to avoid contention with
# the API server's main disk.
VIDEO_LOCAL_ROOT = os.getenv("VIDEO_LOCAL_ROOT", "/var/lib/droptracker/videos").rstrip("/")
VIDEO_LOCAL_RAW_DIR = os.getenv("VIDEO_LOCAL_RAW_DIR", f"{VIDEO_LOCAL_ROOT}/raw").rstrip("/")
VIDEO_LOCAL_FINAL_DIR = os.getenv("VIDEO_LOCAL_FINAL_DIR", f"{VIDEO_LOCAL_ROOT}/final").rstrip("/")
# Keep compatibility with existing VIDEO_LOCAL_RETENTION_HOURS while enforcing
# a strict maximum lifecycle of 1 hour on local files.
_retention_minutes_default = _env_int("VIDEO_LOCAL_RETENTION_HOURS", 1, min_value=1, max_value=1) * 60
VIDEO_LOCAL_RETENTION_MINUTES = _env_int(
    "VIDEO_LOCAL_RETENTION_MINUTES",
    _retention_minutes_default,
    min_value=1,
    max_value=60,
)
VIDEO_LOCAL_RETENTION_HOURS = max(1, VIDEO_LOCAL_RETENTION_MINUTES // 60)
VIDEO_LOCAL_DELETE_AFTER_NOTIFY = os.getenv("VIDEO_LOCAL_DELETE_AFTER_NOTIFY", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def normalize_backend(backend: str | None) -> str:
    backend_name = (backend or VIDEO_STORAGE_BACKEND_DEFAULT).strip().lower()
    if backend_name not in {"b2", "local"}:
        return VIDEO_STORAGE_BACKEND_DEFAULT
    return backend_name


def backend_for_video_record(video_record) -> str:
    return normalize_backend(getattr(video_record, "storage_backend", None))


# The production B2 application key is restricted to object names starting
# with "dt_" (key namePrefix). Keys outside that namespace fail every
# read/write with 403 "not entitled", so all pipeline keys must live under it.
def build_raw_key(player_id: int, video_uuid: str, fps: int) -> str:
    return f"dt_raw/{player_id}/{video_uuid}_fps{fps}.mjpeg"


def derive_final_key(raw_key: str) -> str:
    import re

    key = raw_key
    for prefix in ("dt_raw/", "raw/"):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    key = re.sub(r"_fps\d+\.mjpeg$", ".mp4", key)
    key = re.sub(r"\.mjpeg$", ".mp4", key)
    return f"dt_videos/{key}"


def _safe_join(base_dir: str, relative_path: str) -> str:
    base = Path(base_dir).resolve()
    target = (base / relative_path.lstrip("/")).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"Unsafe key path outside storage root: {relative_path}")
    return str(target)


def resolve_internal_path(object_key: str, backend: str | None = None) -> str:
    backend_name = normalize_backend(backend)
    if backend_name != "local":
        return ""

    for prefix in ("dt_raw/", "raw/"):
        if object_key.startswith(prefix):
            rel = object_key[len(prefix):]
            return _safe_join(VIDEO_LOCAL_RAW_DIR, rel)
    for prefix in ("dt_videos/", "videos/"):
        if object_key.startswith(prefix):
            rel = object_key[len(prefix):]
            return _safe_join(VIDEO_LOCAL_FINAL_DIR, rel)

    # Fallback for unknown prefixes
    return _safe_join(VIDEO_LOCAL_ROOT, object_key)


def get_public_video_url(object_key: str, backend: str | None = None) -> str:
    backend_name = normalize_backend(backend)
    if backend_name == "b2":
        return b2_get_public_video_url(object_key)
    # Local mode uses an internal path only (same-machine consumers).
    return resolve_internal_path(object_key, backend="local")


async def download_to_local(object_key: str, local_path: str, backend: str | None = None) -> bool:
    backend_name = normalize_backend(backend)
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        if backend_name == "b2":
            await b2_download_object(object_key=object_key, local_path=local_path)
            return True

        source_path = resolve_internal_path(object_key, backend="local")
        if not os.path.exists(source_path):
            return False
        await asyncio.to_thread(shutil.copyfile, source_path, local_path)
        return True
    except Exception:
        return False


async def store_final_from_local(
    local_path: str,
    object_key: str,
    backend: str | None = None,
    content_type: str = "video/mp4",
) -> bool:
    backend_name = normalize_backend(backend)
    try:
        if backend_name == "b2":
            await b2_upload_object(local_path=local_path, object_key=object_key, content_type=content_type)
            return True

        target_path = resolve_internal_path(object_key, backend="local")
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, local_path, target_path)
        return True
    except Exception:
        return False


async def delete_object(object_key: str, backend: str | None = None) -> bool:
    backend_name = normalize_backend(backend)
    if backend_name == "b2":
        return await b2_delete_object(object_key)

    try:
        path = resolve_internal_path(object_key, backend="local")
        if os.path.exists(path):
            await asyncio.to_thread(os.remove, path)
        return True
    except Exception:
        return False


async def object_exists(object_key: str, backend: str | None = None) -> bool:
    backend_name = normalize_backend(backend)
    if backend_name == "b2":
        return await b2_object_exists(object_key)
    try:
        return os.path.exists(resolve_internal_path(object_key, backend="local"))
    except Exception:
        return False

