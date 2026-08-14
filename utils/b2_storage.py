"""
Backblaze B2 Cloud Storage Utility

Provides presigned URL generation and object management for the video upload pipeline.
Uses the S3-compatible API via boto3 so the same code works with any S3-compatible provider.

Environment variables required:
    B2_KEY_ID: Backblaze B2 application key ID
    B2_APPLICATION_KEY: Backblaze B2 application key
    B2_BUCKET_NAME: Target bucket name (e.g. "droptracker-videos")
    B2_ENDPOINT_URL: S3-compatible endpoint (e.g. "https://s3.us-west-004.backblazeb2.com")
    B2_CDN_BASE_URL: Public CDN base URL for serving videos (e.g. "https://videos.droptracker.io")
"""

import os
import asyncio
from typing import Optional

import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

# Configuration from environment
B2_KEY_ID = os.getenv("B2_KEY_ID", "")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "droptracker-videos")
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
B2_CDN_BASE_URL = os.getenv("B2_CDN_BASE_URL", "").rstrip("/")

# Presigned URL expiry (seconds)
PRESIGNED_URL_EXPIRY = 600  # 10 minutes

# Lazy-initialized S3 client
_s3_client = None


def _get_s3_client():
    """Get or create the singleton boto3 S3 client for B2."""
    global _s3_client
    if _s3_client is None:
        if not B2_KEY_ID or not B2_APPLICATION_KEY:
            raise RuntimeError(
                "B2 credentials not configured. "
                "Set B2_KEY_ID and B2_APPLICATION_KEY environment variables."
            )
        _s3_client = boto3.client(
            "s3",
            endpoint_url=B2_ENDPOINT_URL,
            aws_access_key_id=B2_KEY_ID,
            aws_secret_access_key=B2_APPLICATION_KEY,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )
    return _s3_client


def generate_presigned_upload_url(
    object_key: str,
    content_type: str | None = None,
    expiry_seconds: int = PRESIGNED_URL_EXPIRY,
) -> str:
    """
    Generate a presigned PUT URL for direct-to-B2 upload.

    The client uses this URL to upload the raw MJPEG directly to B2 
    without routing the data through our API server.

    Args:
        object_key: The full object key (e.g. "raw/12345/uuid_fps20.mjpeg")
        content_type: MIME type for the upload
        expiry_seconds: How long the URL is valid (default 10 minutes)

    Returns:
        Presigned PUT URL string
    """
    client = _get_s3_client()
    params = {
        "Bucket": B2_BUCKET_NAME,
        "Key": object_key,
    }
    # IMPORTANT: If we include ContentType in the signed params, the uploader must
    # send an identical Content-Type header or B2 will reject with signature mismatch.
    # Many clients omit/alter this header, so we only sign it when explicitly desired.
    if content_type:
        params["ContentType"] = content_type

    url = client.generate_presigned_url(
        ClientMethod="put_object",
        Params=params,
        ExpiresIn=expiry_seconds,
    )
    return url


def generate_presigned_download_url(
    object_key: str,
    expiry_seconds: int = 3600,
) -> str:
    """
    Generate a presigned GET URL for downloading an object from B2.

    Args:
        object_key: The full object key (e.g. "videos/12345/uuid.mp4")
        expiry_seconds: How long the URL is valid (default 1 hour)

    Returns:
        Presigned GET URL string
    """
    client = _get_s3_client()
    url = client.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": B2_BUCKET_NAME,
            "Key": object_key,
        },
        ExpiresIn=expiry_seconds,
    )
    return url


def get_public_video_url(object_key: str) -> str:
    """
    Get the public CDN URL for a processed video.

    If B2_CDN_BASE_URL is configured (Cloudflare in front of B2),
    returns the CDN URL. Otherwise falls back to a presigned download URL.

    Args:
        object_key: The full object key (e.g. "videos/12345/uuid.mp4")

    Returns:
        Public URL string for the video
    """
    if B2_CDN_BASE_URL:
        return f"{B2_CDN_BASE_URL}/{object_key}"
    return generate_presigned_download_url(object_key)


async def download_object(object_key: str, local_path: str) -> str:
    """
    Download an object from B2 to a local file path.

    Args:
        object_key: The full object key in B2
        local_path: Local filesystem path to save the file

    Returns:
        The local_path on success

    Raises:
        Exception on download failure
    """
    client = _get_s3_client()
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    await asyncio.to_thread(
        client.download_file,
        B2_BUCKET_NAME,
        object_key,
        local_path,
    )
    return local_path


async def upload_object(
    local_path: str,
    object_key: str,
    content_type: str = "video/mp4",
) -> str:
    """
    Upload a local file to B2.

    Args:
        local_path: Local filesystem path of the file to upload
        object_key: Target object key in B2
        content_type: MIME type for the upload

    Returns:
        The object_key on success
    """
    client = _get_s3_client()
    await asyncio.to_thread(
        client.upload_file,
        local_path,
        B2_BUCKET_NAME,
        object_key,
        ExtraArgs={"ContentType": content_type},
    )
    return object_key


async def upload_bytes(
    data: bytes,
    object_key: str,
    content_type: str,
) -> str:
    """
    Upload raw bytes to B2 server-side (no presigned URL, no browser CORS).

    Used for proof-media uploads that the browser POSTs to our own server: the
    server streams them here with its credentials, so the bucket does not need a
    CORS PUT rule (Backblaze's policy only permits GET/HEAD, which is why a
    direct browser→B2 PUT failed the CORS preflight).

    Args:
        data: The raw file bytes to store.
        object_key: Target object key in B2 (e.g. "dt_uploads/uuid.png").
        content_type: MIME type stored on the object (drives how the CDN serves it).

    Returns:
        The object_key on success.
    """
    client = _get_s3_client()
    await asyncio.to_thread(
        lambda: client.put_object(
            Bucket=B2_BUCKET_NAME,
            Key=object_key,
            Body=data,
            ContentType=content_type,
        )
    )
    return object_key


async def download_bytes(object_key: str) -> bytes:
    """Read an object back into memory.

    The counterpart to `upload_bytes`, for private objects that must never get
    a public URL: the file-transfer routes stream these bytes out through an
    authed endpoint instead of handing the browser a CDN link. Callers are
    responsible for keeping the objects small enough to hold in memory (the
    transfer routes cap uploads at 25 MB).

    Args:
        object_key: The full object key in B2.

    Returns:
        The object's raw bytes.

    Raises:
        Exception if the object is missing or unreadable.
    """
    client = _get_s3_client()

    def _read() -> bytes:
        obj = client.get_object(Bucket=B2_BUCKET_NAME, Key=object_key)
        return obj["Body"].read()

    return await asyncio.to_thread(_read)


async def delete_object(object_key: str) -> bool:
    """
    Delete an object from B2.

    Args:
        object_key: The full object key to delete

    Returns:
        True on success, False on failure
    """
    try:
        client = _get_s3_client()
        await asyncio.to_thread(
            client.delete_object,
            Bucket=B2_BUCKET_NAME,
            Key=object_key,
        )
        return True
    except Exception as e:
        print(f"[B2] Error deleting object {object_key}: {e}")
        return False


async def object_exists(object_key: str) -> bool:
    """
    Check if an object exists in B2.

    Args:
        object_key: The full object key to check

    Returns:
        True if object exists, False otherwise
    """
    try:
        client = _get_s3_client()
        await asyncio.to_thread(
            client.head_object,
            Bucket=B2_BUCKET_NAME,
            Key=object_key,
        )
        return True
    except Exception:
        return False
