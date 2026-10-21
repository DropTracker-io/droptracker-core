"""Data-removal requests: a player or a whole group asking to be erased (web137a).

Both kinds go the same way. Someone asks on the site (typing the name to
confirm), which opens a support ticket; staff verify ownership in that ticket
and approve it; the data is then erased after a grace period during which the
request can still be cancelled. See ``services/data_removal.py``.

Status flow::

    awaiting_verification --approve--> scheduled --(purge_after)--> completed
            |                              |
            +--reject/cancel--> rejected / cancelled <--cancel--+
                                           scheduled --error--> failed

The row outlives the data on purpose: it is the record that a removal was
asked for, by whom, approved by whom and when it ran. It holds names and ids,
never the erased data itself; that lives only in the time-limited export.
"""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Text, func

from .base import Base

KIND_PLAYER = "player"
KIND_GROUP = "group"

AWAITING = "awaiting_verification"
SCHEDULED = "scheduled"
COMPLETED = "completed"
CANCELLED = "cancelled"
REJECTED = "rejected"
FAILED = "failed"

OPEN_STATUSES = (AWAITING, SCHEDULED)


class DataRemovalRequest(Base):
    __tablename__ = "data_removal_requests"
    __table_args__ = (
        Index("idx_data_removal_subject", "kind", "subject_id"),
        Index("idx_data_removal_due", "status", "purge_after"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(16), nullable=False)          # player | group
    # No FK: the subject is deleted by the request it belongs to.
    subject_id = Column(Integer, nullable=False)
    subject_name = Column(String(120), nullable=False)
    status = Column(String(24), nullable=False, default=AWAITING)
    requested_by_user_id = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"),
                                  nullable=True)
    ticket_id = Column(Integer, nullable=True)
    # Shown in the ticket: a player types it in the game chatbox and sends a
    # screenshot of their whole client, which proves they control the account.
    verification_phrase = Column(String(64), nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    purge_after = Column(DateTime, nullable=True)
    # players.hidden before approval, so a cancel puts it back exactly.
    prior_hidden = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    export_path = Column(String(300), nullable=True)
    export_bytes = Column(BigInteger, nullable=True)
    export_expires_at = Column(DateTime, nullable=True)
    result = Column(Text, nullable=True)  # JSON summary of what ran, or the error
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
