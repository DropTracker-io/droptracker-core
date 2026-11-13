"""Requests for a Data API key (web163a).

A group admin (from the group's dashboard) or any signed-in user (from their
account) asks for a key and says what it is for. Asking opens an
``api_token`` support ticket; staff approve or deny there with buttons, or
from ``/admin/api-keys``. Approval mints the key and hands the requester a
one-time claim link (``api_key_reveals``). See ``db/api_key_requests.py``.

Status flow::

    pending --approve--> approved   (api_key_id set)
       |
       +--deny--> denied
       +--cancel (requester)--> cancelled

The row is kept after the decision: it is the record of who asked for a key,
what they said it was for, and who let them have it.
"""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Text, func

from .base import Base

SCOPE_USER = "user"
SCOPE_GROUP = "group"

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
CANCELLED = "cancelled"


class ApiKeyRequest(Base):
    __tablename__ = "api_key_requests"
    __table_args__ = (
        Index("idx_api_key_requests_status", "status", "id"),
        Index("idx_api_key_requests_user", "requested_by_user_id"),
        Index("idx_api_key_requests_group", "group_id"),
        Index("idx_api_key_requests_ticket", "ticket_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(16), nullable=False)             # user | group
    #: Set for a group request; the key will read this group's members.
    group_id = Column(Integer, nullable=True)
    requested_by_user_id = Column(
        Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    #: What the requester calls the thing they are building; becomes the key's label.
    label = Column(String(64), nullable=False, default="")
    #: What they are building and how often it will call us, in their words.
    purpose = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default=PENDING, server_default=PENDING)
    ticket_id = Column(Integer, nullable=True)
    api_key_id = Column(BigInteger, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
