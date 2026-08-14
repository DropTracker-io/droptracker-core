"""Private file transfers between a signed-in user and site staff (web95a).

Backs the unlisted ``/file-transfer`` page (any signed-in user, reachable only
by direct URL — there is no nav link to it) and its ``/admin/file-transfers``
counterpart in the staff CP.

  POST   /api/v1/file-transfers                              -> Transfer   (any user)
  GET    /api/v1/file-transfers                              -> Transfer[] (own only)
  GET    /api/v1/file-transfers/{id}/versions/{v}/download   -> raw bytes  (owner|staff)
  GET    /api/v1/admin/file-transfers                        -> Transfer[] (staff)
  POST   /api/v1/admin/file-transfers/{id}/versions          -> Transfer   (staff)
  DELETE /api/v1/admin/file-transfers/{id}                   -> { ok }     (staff)

Staff here means developer-or-superadmin (``assert_developer``), matching the
``/admin`` shell's own gate.

Three things worth keeping in mind when editing this module:

* **Bytes live in B2, not on disk.** The box's root filesystem sits at ~97%
  full and holds no backup of user uploads, so a 25 MB general-purpose upload
  channel could not live there. Objects go under the private ``dt_transfers/``
  key prefix (the B2 application key is namePrefix-restricted to ``dt_``;
  anything outside that namespace 403s as "not entitled").
* **No object is ever exposed by URL.** Downloads stream through the authed
  endpoint below, so knowing a key grants nothing. That is what makes this safe
  for arbitrary user files, which the CDN-backed proof-image path is not.
* **Arbitrary file types are the point**, so nothing here may be served in a
  way the browser will execute. Content-Type is normalised against an
  allowlist, everything unrecognised becomes ``application/octet-stream``, and
  only a small set of obviously-inert types may render inline (never SVG or
  HTML). See ``_safe_content_type`` / ``_disposition``.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from quart import Blueprint, Response, jsonify, request
from sqlalchemy.orm import joinedload

from db import (
    FileTransfer,
    FileTransferVersion,
    TRANSFER_MAX_BYTES,
    TRANSFER_RETENTION_DAYS,
    User,
)
from web_api.common import abort_problem, db_session, parse_page, private_no_store
from web_api.deps import assert_developer, current_user_id, is_developer, load_user

file_transfers_bp = Blueprint("v1_file_transfers", __name__)

#: B2 key namespace. Must start with "dt_" — the application key is
#: namePrefix-restricted and rejects anything else.
_KEY_PREFIX = "dt_transfers/"

#: Content types we will hand back with ``Content-Disposition: inline`` so the
#: admin can preview without downloading. Deliberately excludes SVG and any
#: text/html flavour: those execute script in our origin. Everything not listed
#: is force-downloaded, which is the safe default for a general file channel.
_INLINE_SAFE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "video/mp4",
        "audio/mpeg",
    }
)

#: A conservative MIME token — letters, digits and ``.+-`` either side of one
#: slash. Anything else (parameters, control characters, header-splitting
#: attempts) is discarded rather than sanitised, since we have a safe default.
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,62}/[a-z0-9][a-z0-9.+-]{0,62}$")

_DEFAULT_TYPE = "application/octet-stream"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested in tests/unit/test_file_transfers.py)
# --------------------------------------------------------------------------- #
def safe_filename(raw: Optional[str]) -> str:
    """Reduce a browser-supplied filename to something safe to store and echo.

    The name is only ever used as a label and in a Content-Disposition header —
    never as a filesystem path — but it still must not carry path separators
    (which turn a download into a directory traversal on the *client* side),
    control characters, or the quotes/newlines that would let it break out of
    the header it is quoted into.
    """
    name = (raw or "").strip()
    # Browsers on some platforms send a full path; keep the last component of
    # either separator style.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Strip control characters and quote/newline header-injection vectors.
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = name.replace('"', "").replace("'", "").strip()
    # Leading dots would produce a hidden file if the recipient saves it as-is.
    name = name.lstrip(".") or ""
    if not name:
        return "upload.bin"
    return name[:255]


def safe_content_type(raw: Optional[str]) -> str:
    """Normalise a client-declared MIME type, or fall back to octet-stream.

    We never trust the declared type for *safety* decisions beyond this: the
    value that survives is either a well-formed token we chose to keep or the
    inert default, so it can be written into a response header directly.
    """
    value = (raw or "").split(";", 1)[0].strip().lower()
    if not value or not _MIME_RE.match(value):
        return _DEFAULT_TYPE
    return value


def disposition(filename: str, content_type: str, *, want_inline: bool) -> str:
    """Build the Content-Disposition header value for a download.

    Inline is granted only for the small allowlist of inert types AND only when
    the caller asked for a preview; everything else is an attachment. The name
    is sent twice — a plain ASCII fallback plus RFC 5987 ``filename*`` — so
    non-ASCII names survive without putting raw bytes in the quoted form.
    """
    mode = "inline" if (want_inline and content_type in _INLINE_SAFE_TYPES) else "attachment"
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return f"{mode}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def new_storage_key() -> str:
    """A fresh, unguessable object key inside the transfers namespace."""
    return f"{_KEY_PREFIX}{uuid.uuid4().hex}"


def expiry_from(moment: datetime) -> datetime:
    """When a transfer whose newest version landed at ``moment`` should be pruned."""
    return moment + timedelta(days=TRANSFER_RETENTION_DAYS)


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def _ts(dt: Optional[datetime]) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def _version_row(v: FileTransferVersion, names: dict) -> dict:
    return {
        "id": v.id,
        "version": int(v.version),
        "filename": v.filename,
        "content_type": v.content_type,
        "size_bytes": int(v.size_bytes or 0),
        "uploaded_by": v.uploaded_by_user_id,
        "uploaded_by_name": names.get(v.uploaded_by_user_id),
        "uploaded_by_role": v.uploaded_by_role,
        "can_preview": v.content_type in _INLINE_SAFE_TYPES,
        "created_at": _ts(v.created_at),
    }


def _transfer_row(t: FileTransfer, names: dict) -> dict:
    versions = sorted(t.versions, key=lambda v: v.version)
    return {
        "id": t.id,
        "title": t.title,
        "note": t.note,
        "owner_user_id": t.owner_user_id,
        "owner_name": names.get(t.owner_user_id),
        "latest_version": int(t.latest_version or 1),
        "created_at": _ts(t.created_at),
        "updated_at": _ts(t.updated_at),
        "expires_at": _ts(t.expires_at),
        "versions": [_version_row(v, names) for v in versions],
    }


def _name_map(s, transfers: list[FileTransfer]) -> dict:
    """user_id -> username for every owner/uploader across the given rows."""
    ids = set()
    for t in transfers:
        ids.add(t.owner_user_id)
        for v in t.versions:
            ids.add(v.uploaded_by_user_id)
    ids.discard(None)
    if not ids:
        return {}
    rows = s.query(User.user_id, User.username).filter(User.user_id.in_(ids)).all()
    return {uid: name for uid, name in rows}


def _live(query):
    """Hide rows whose retention window has already passed.

    The pruner is what actually deletes them, but it runs daily — without this
    a transfer would stay listed and downloadable for up to a day after the
    30-day promise expired.
    """
    return query.filter(FileTransfer.expires_at > datetime.now())


def _load_transfer(s, transfer_id: int) -> FileTransfer:
    t = (
        s.query(FileTransfer)
        .options(joinedload(FileTransfer.versions))
        .filter(FileTransfer.id == transfer_id)
        .first()
    )
    if t is None or t.expires_at <= datetime.now():
        abort_problem(404, "Not found", "No such file transfer.")
    return t


async def _read_upload():
    """Pull the multipart 'file' field, enforcing the size cap.

    Quart's own ``MAX_CONTENT_LENGTH`` (raised to accommodate this endpoint in
    ``web_api/__init__``) rejects grossly oversized bodies before we see them;
    this is the per-file check, and it is the one whose message users read.
    """
    files = await request.files
    upload = files.get("file")
    if upload is None:
        abort_problem(422, "Invalid body", "A multipart 'file' field is required.")
    raw = upload.read()
    if not raw:
        abort_problem(422, "Empty file", "The uploaded file was empty.")
    if len(raw) > TRANSFER_MAX_BYTES:
        abort_problem(422, "File too large", "Uploads are capped at 25 MB.")
    return raw, upload


async def _store(raw: bytes, content_type: str) -> str:
    from utils.b2_storage import upload_bytes

    key = new_storage_key()
    try:
        await upload_bytes(raw, key, content_type)
    except Exception as e:
        abort_problem(502, "Upload service unavailable", str(e))
    return key


# --------------------------------------------------------------------------- #
# User surface
# --------------------------------------------------------------------------- #
@file_transfers_bp.post("/file-transfers")
async def create_transfer():
    """Start a transfer: stores the file as version 1 and returns the row."""
    user_id = current_user_id()
    raw, upload = await _read_upload()

    form = await request.form
    filename = safe_filename(upload.filename)
    content_type = safe_content_type(upload.content_type)
    note = (form.get("note") or "").strip()[:2000] or None
    size = len(raw)

    key = await _store(raw, content_type)

    def _write():
        now = datetime.now()
        with db_session() as s:
            transfer = FileTransfer(
                owner_user_id=user_id,
                title=filename,
                note=note,
                latest_version=1,
                created_at=now,
                updated_at=now,
                expires_at=expiry_from(now),
            )
            s.add(transfer)
            s.flush()
            s.add(
                FileTransferVersion(
                    transfer_id=transfer.id,
                    version=1,
                    uploaded_by_user_id=user_id,
                    uploaded_by_role="user",
                    filename=filename,
                    content_type=content_type,
                    size_bytes=size,
                    storage_key=key,
                    created_at=now,
                )
            )
            s.commit()
            transfer = _load_transfer(s, transfer.id)
            return _transfer_row(transfer, _name_map(s, [transfer]))

    return private_no_store(jsonify(await asyncio.to_thread(_write)))


@file_transfers_bp.get("/file-transfers")
async def my_transfers():
    """Every transfer the caller owns, newest first, with all its versions."""
    user_id = current_user_id()
    page, limit = parse_page(request, default_limit=25, max_limit=100)

    def _load():
        with db_session() as s:
            query = _live(
                s.query(FileTransfer)
                .options(joinedload(FileTransfer.versions))
                .filter(FileTransfer.owner_user_id == user_id)
            ).order_by(FileTransfer.created_at.desc())
            total = query.count()
            rows = query.offset((page - 1) * limit).limit(limit).all()
            names = _name_map(s, rows)
            return {
                "items": [_transfer_row(t, names) for t in rows],
                "meta": {"page": page, "limit": limit, "total": int(total)},
                "max_bytes": TRANSFER_MAX_BYTES,
                "retention_days": TRANSFER_RETENTION_DAYS,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@file_transfers_bp.get("/file-transfers/<int:transfer_id>/versions/<int:version>/download")
async def download_version(transfer_id: int, version: int):
    """Stream one version's bytes to the owner or to staff.

    Deliberately not a redirect to B2: the objects have no public URL, and
    issuing one (even presigned) would leak a credential-free handle that
    outlives the session check we just made.
    """
    user_id = current_user_id()
    want_inline = request.args.get("inline") in ("1", "true", "yes")

    def _authorize():
        with db_session() as s:
            transfer = _load_transfer(s, transfer_id)
            if transfer.owner_user_id != user_id and not is_developer(load_user(s, user_id)):
                abort_problem(403, "Forbidden", "This file is not yours.")
            row = next((v for v in transfer.versions if v.version == version), None)
            if row is None:
                abort_problem(404, "Not found", "No such version of this file.")
            return row.storage_key, row.filename, row.content_type

    key, filename, content_type = await asyncio.to_thread(_authorize)

    from utils.b2_storage import download_bytes

    try:
        data = await download_bytes(key)
    except Exception as e:
        abort_problem(502, "Storage unavailable", str(e))

    return Response(
        data,
        content_type=content_type,
        headers={
            "Content-Disposition": disposition(filename, content_type, want_inline=want_inline),
            "Content-Length": str(len(data)),
            # Belt and braces alongside the type allowlist: never let the
            # browser sniff a friendlier (executable) type out of the bytes.
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# --------------------------------------------------------------------------- #
# Staff surface (developer or superadmin)
# --------------------------------------------------------------------------- #
@file_transfers_bp.get("/admin/file-transfers")
async def admin_transfers():
    """Every live transfer from every user, newest activity first."""
    user_id = current_user_id()
    page, limit = parse_page(request, default_limit=50, max_limit=200)

    def _load():
        with db_session() as s:
            assert_developer(load_user(s, user_id))
            query = _live(
                s.query(FileTransfer).options(joinedload(FileTransfer.versions))
            ).order_by(FileTransfer.updated_at.desc())
            total = query.count()
            rows = query.offset((page - 1) * limit).limit(limit).all()
            names = _name_map(s, rows)
            return {
                "items": [_transfer_row(t, names) for t in rows],
                "meta": {"page": page, "limit": limit, "total": int(total)},
                "max_bytes": TRANSFER_MAX_BYTES,
                "retention_days": TRANSFER_RETENTION_DAYS,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@file_transfers_bp.post("/admin/file-transfers/<int:transfer_id>/versions")
async def add_staff_version(transfer_id: int):
    """Answer a transfer with an updated copy — becomes the next version.

    Also pushes the retention window out by another 30 days from now, so a
    staff reply on day 29 doesn't vanish overnight.
    """
    user_id = current_user_id()

    def _check():
        with db_session() as s:
            assert_developer(load_user(s, user_id))
            _load_transfer(s, transfer_id)

    await asyncio.to_thread(_check)

    raw, upload = await _read_upload()
    filename = safe_filename(upload.filename)
    content_type = safe_content_type(upload.content_type)
    size = len(raw)
    key = await _store(raw, content_type)

    def _write():
        now = datetime.now()
        with db_session() as s:
            transfer = _load_transfer(s, transfer_id)
            version = int(transfer.latest_version or 1) + 1
            s.add(
                FileTransferVersion(
                    transfer_id=transfer.id,
                    version=version,
                    uploaded_by_user_id=user_id,
                    uploaded_by_role="staff",
                    filename=filename,
                    content_type=content_type,
                    size_bytes=size,
                    storage_key=key,
                    created_at=now,
                )
            )
            transfer.latest_version = version
            transfer.updated_at = now
            transfer.expires_at = expiry_from(now)
            s.commit()
            transfer = _load_transfer(s, transfer_id)
            return _transfer_row(transfer, _name_map(s, [transfer]))

    return private_no_store(jsonify(await asyncio.to_thread(_write)))


@file_transfers_bp.delete("/admin/file-transfers/<int:transfer_id>")
async def delete_transfer(transfer_id: int):
    """Drop a transfer and every version's object ahead of its expiry."""
    user_id = current_user_id()

    def _collect():
        with db_session() as s:
            assert_developer(load_user(s, user_id))
            transfer = _load_transfer(s, transfer_id)
            return [v.storage_key for v in transfer.versions]

    keys = await asyncio.to_thread(_collect)

    from utils.b2_storage import delete_object

    for key in keys:
        # Best-effort: a storage failure must not strand the DB rows, and the
        # pruner's sweep is not a second chance once the row is gone.
        await delete_object(key)

    def _drop():
        with db_session() as s:
            transfer = s.query(FileTransfer).filter(FileTransfer.id == transfer_id).first()
            if transfer is not None:
                s.delete(transfer)
                s.commit()

    await asyncio.to_thread(_drop)
    return private_no_store(jsonify({"ok": True}))
