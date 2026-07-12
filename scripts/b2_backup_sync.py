#!/usr/bin/env python
"""
Backblaze B2 sync helper for the nightly database backup job.

Used by scripts/db_backup.sh (run via venv/bin/python). Mirrors the boto3
S3-compatible client setup in utils/b2_storage.py, but is deliberately
standalone so the backup job has no import-time dependency on the app.

IMPORTANT: the production B2 application key is restricted to the key
namePrefix ``dt_`` — every object key MUST start with ``dt_`` or B2 returns
401. All backup objects live under ``dt_backups/mysql/YYYY-MM-DD/<file>``.

Subcommands:
    check                          Read-only: list the backup prefix, print a summary.
    upload <file> <object_key>     Upload one local file to <object_key>.
    upload-dir <dir> <key_prefix>  Upload every regular file in <dir> under <key_prefix>/.
    download <object_key> <file>   Download one object (for disaster recovery).
    prune --days N [--dry-run]     Delete backup objects older than N days.

Safety rails:
    * upload/upload-dir refuse any key that does not start with ``dt_backups/``.
    * prune is HARDCODED to the ``dt_backups/`` prefix — it can never touch
      the video objects (``dt_videos/...``, ``dt_raw/...``) that share the bucket.

Environment (read from /store/droptracker/disc/.env):
    B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME, B2_ENDPOINT_URL
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

B2_KEY_ID = os.getenv("B2_KEY_ID", "")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "")
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")

# All backup objects live under this prefix. The B2 key is restricted to the
# namePrefix "dt_", and this constant additionally fences the prune/upload
# logic away from the video pipeline's objects (dt_videos/, dt_raw/).
BACKUP_PREFIX = "dt_backups/"

# Objects whose key embeds a date directory, e.g. dt_backups/mysql/2026-07-12/...
DATE_IN_KEY = re.compile(r"/(\d{4}-\d{2}-\d{2})/")


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} [b2] {msg}", flush=True)


def fail(msg: str) -> "None":
    log(f"FATAL: {msg}")
    sys.exit(1)


def get_client():
    if not B2_KEY_ID or not B2_APPLICATION_KEY:
        fail("B2 credentials not configured (B2_KEY_ID / B2_APPLICATION_KEY missing from .env)")
    if not B2_BUCKET_NAME:
        fail("B2_BUCKET_NAME missing from .env")
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def guard_key(key: str) -> None:
    if not key.startswith(BACKUP_PREFIX):
        fail(
            f"refusing to operate on key {key!r}: backup objects must live under "
            f"{BACKUP_PREFIX!r} (B2 key is also namePrefix-restricted to 'dt_')"
        )


def content_type_for(path: Path) -> str:
    name = path.name
    if name.endswith(".sql.gz") or name.endswith(".rdb.gz") or name.endswith(".gz"):
        return "application/gzip"
    if name.endswith(".sql"):
        return "application/sql"
    return "application/octet-stream"


def cmd_check(_args) -> None:
    client = get_client()
    resp = client.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=BACKUP_PREFIX, MaxKeys=1000)
    objs = resp.get("Contents", [])
    total = sum(o["Size"] for o in objs)
    log(f"bucket={B2_BUCKET_NAME} prefix={BACKUP_PREFIX} objects={len(objs)}"
        f"{'+' if resp.get('IsTruncated') else ''} bytes={total}")
    for o in objs[-5:]:
        log(f"  {o['Key']}  {o['Size']} bytes  {o['LastModified'].isoformat()}")
    log("check OK")


def _upload_one(client, path: Path, key: str) -> None:
    guard_key(key)
    if not path.is_file():
        fail(f"local file not found: {path}")
    size = path.stat().st_size
    log(f"uploading {path} -> s3://{B2_BUCKET_NAME}/{key} ({size} bytes)")
    client.upload_file(
        str(path),
        B2_BUCKET_NAME,
        key,
        ExtraArgs={"ContentType": content_type_for(path)},
    )
    # Verify the object landed with the expected size.
    head = client.head_object(Bucket=B2_BUCKET_NAME, Key=key)
    if head["ContentLength"] != size:
        fail(f"size mismatch after upload of {key}: local={size} remote={head['ContentLength']}")
    log(f"uploaded {key} OK")


def cmd_upload(args) -> None:
    client = get_client()
    _upload_one(client, Path(args.file), args.key)


def cmd_upload_dir(args) -> None:
    client = get_client()
    prefix = args.key_prefix.rstrip("/")
    guard_key(prefix + "/")
    src = Path(args.dir)
    if not src.is_dir():
        fail(f"local directory not found: {src}")
    files = sorted(p for p in src.iterdir() if p.is_file())
    if not files:
        fail(f"no files to upload in {src}")
    for p in files:
        _upload_one(client, p, f"{prefix}/{p.name}")
    log(f"upload-dir OK ({len(files)} files)")


def cmd_download(args) -> None:
    client = get_client()
    guard_key(args.key)
    dest = Path(args.file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading s3://{B2_BUCKET_NAME}/{args.key} -> {dest}")
    client.download_file(B2_BUCKET_NAME, args.key, str(dest))
    log(f"downloaded {dest} ({dest.stat().st_size} bytes) OK")


def cmd_prune(args) -> None:
    if args.days < 7:
        fail("prune --days must be >= 7 (safety floor)")
    client = get_client()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    log(f"pruning objects under {BACKUP_PREFIX!r} older than {args.days} days "
        f"(cutoff {cutoff.date().isoformat()}){' [dry-run]' if args.dry_run else ''}")

    doomed = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=B2_BUCKET_NAME, Prefix=BACKUP_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Belt and braces: never consider anything outside the backup prefix.
            if not key.startswith(BACKUP_PREFIX):
                continue
            m = DATE_IN_KEY.search(key)
            if m:
                try:
                    obj_date = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    obj_date = obj["LastModified"]
            else:
                obj_date = obj["LastModified"]
            if obj_date < cutoff:
                doomed.append(key)

    if not doomed:
        log("prune: nothing to delete")
        return
    for key in doomed:
        log(f"prune: {'would delete' if args.dry_run else 'deleting'} {key}")
    if args.dry_run:
        return
    for i in range(0, len(doomed), 1000):
        batch = doomed[i:i + 1000]
        resp = client.delete_objects(
            Bucket=B2_BUCKET_NAME,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        errors = resp.get("Errors", [])
        if errors:
            for e in errors:
                log(f"prune ERROR: {e.get('Key')}: {e.get('Code')} {e.get('Message')}")
            fail(f"prune failed for {len(errors)} object(s)")
    log(f"prune OK ({len(doomed)} objects deleted)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="read-only connectivity/listing check")

    p = sub.add_parser("upload", help="upload one file")
    p.add_argument("file")
    p.add_argument("key")

    p = sub.add_parser("upload-dir", help="upload every file in a directory")
    p.add_argument("dir")
    p.add_argument("key_prefix")

    p = sub.add_parser("download", help="download one object (disaster recovery)")
    p.add_argument("key")
    p.add_argument("file")

    p = sub.add_parser("prune", help="delete backup objects older than N days")
    p.add_argument("--days", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    {
        "check": cmd_check,
        "upload": cmd_upload,
        "upload-dir": cmd_upload_dir,
        "download": cmd_download,
        "prune": cmd_prune,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
