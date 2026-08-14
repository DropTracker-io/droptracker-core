"""Direct-URL file transfers (web95a).

A private hand-off channel between a signed-in user and site staff: the user
uploads a file at the unlisted ``/file-transfer`` page, staff see it in the
admin CP, and either side can then download any version. Staff can answer with
a corrected/updated copy of the same file, which lands as the next **version**
of the same transfer rather than as a separate upload — that versioning is the
whole point of the feature, so the transfer row is a thin envelope and every
actual file lives in ``file_transfer_versions``.

Bytes live in Backblaze B2 under the private ``dt_transfers/`` key prefix (the
box's disk has no room for them and is not backed up anyway). Nothing here is
ever served from the public CDN base — downloads stream through the authed
``/api/v1/file-transfers/...`` endpoints, so an object key leaking is not by
itself an access grant.

Retention: rows carry ``expires_at``, refreshed to +30d whenever a new version
is added, so a staff reply can't be pruned out from under the user the next
morning. ``scripts/prune_file_transfers.py`` (daily timer) deletes the B2
objects and the rows once that stamp passes; the read routes also hide expired
rows so a late sweep can't resurrect one.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from .base import Base

# Who put a given version in. 'user' = the transfer's owner, 'staff' = an
# admin answering with an updated copy. Stored per-version because the owner
# may add versions after staff already replied.
TRANSFER_UPLOADER_ROLES = ("user", "staff")

#: Hard cap enforced by the upload route and mirrored by the browser picker.
TRANSFER_MAX_BYTES = 25 * 1024 * 1024

#: Days a transfer survives after its most recent version.
TRANSFER_RETENTION_DAYS = 30


class FileTransfer(Base):
    """One file hand-off, owned by the user who started it."""

    __tablename__ = "file_transfers"
    __table_args__ = (
        Index("idx_file_transfer_owner", "owner_user_id", "created_at"),
        # The pruner and the admin list both sort/filter on this.
        Index("idx_file_transfer_expires", "expires_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    # Denormalised from the first version so the admin list can render without
    # joining, and so the label survives if versions are ever pruned per-file.
    title = Column(String(255), nullable=False)
    note = Column(Text, nullable=True)
    latest_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime, nullable=False)

    versions = relationship(
        "FileTransferVersion",
        back_populates="transfer",
        cascade="all, delete-orphan",
        order_by="FileTransferVersion.version",
    )


class FileTransferVersion(Base):
    """One uploaded revision of a transfer. Version 1 is the user's original."""

    __tablename__ = "file_transfer_versions"
    __table_args__ = (
        Index("idx_file_transfer_version_transfer", "transfer_id", "version"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    transfer_id = Column(
        Integer, ForeignKey("file_transfers.id", ondelete="CASCADE"), nullable=False
    )
    version = Column(Integer, nullable=False, default=1)
    uploaded_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    uploaded_by_role = Column(String(8), nullable=False, default="user")
    # The name the uploader's browser sent, kept for display and for the
    # Content-Disposition on download. Sanitised at write time — never
    # interpolated into a filesystem path (the bytes live in B2).
    filename = Column(String(255), nullable=False)
    # Validated against a strict token regex before storage; anything unknown
    # is recorded as application/octet-stream rather than echoed back verbatim.
    content_type = Column(String(128), nullable=False, default="application/octet-stream")
    size_bytes = Column(Integer, nullable=False, default=0)
    #: B2 object key (``dt_transfers/<uuid4hex>``). Not a public URL.
    storage_key = Column(String(255), nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False, default=func.now())

    transfer = relationship("FileTransfer", back_populates="versions")
