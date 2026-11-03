"""Clan leader feedback (web155a).

Staff ask chosen people for feedback from /admin/feedback. A request carries
an audience in the same rule format as pop-up notices
(``web_api/popup_audience``), and a signed-in visitor who matches sees a small
feedback card until they answer, snooze it or turn it down.

Every answer is stored here and posted to the staff "raw" Discord channel,
where a staff member can publish it to the testimonials channel
(``services/feedback_buttons_discord.py``).
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)

from .base import Base


class FeedbackRequest(Base):
    """One round of asking. ``status`` is draft (never shown), live (shown to
    matching visitors) or ended. ``title`` is a staff-only name for the round
    and is never shown to the people asked."""

    __tablename__ = "feedback_requests"
    __table_args__ = (
        Index("idx_feedback_request_status", "status"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(120), nullable=False)
    audience_json = Column(Text, nullable=False)
    status = Column(String(12), nullable=False, default="draft")  # draft|live|ended
    audience_estimate = Column(Integer, nullable=True)
    created_by = Column(Integer, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class FeedbackReceipt(Base):
    """One person's history with one request: when the card was first shown,
    and whether they answered, snoozed it ("Not now") or turned it down for
    good ("Don't ask again")."""

    __tablename__ = "feedback_receipts"
    __table_args__ = (
        UniqueConstraint("request_id", "user_id", name="uix_feedback_receipt"),
        Index("idx_feedback_receipt_user", "user_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(Integer, ForeignKey("feedback_requests.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, nullable=False)
    seen_at = Column(DateTime, nullable=True)
    snoozed_until = Column(DateTime, nullable=True)
    declined_at = Column(DateTime, nullable=True)
    responded_at = Column(DateTime, nullable=True)


class FeedbackResponse(Base):
    """One answer. ``request_id`` is NULL when it came from the always-on
    "Share feedback" page rather than a request.

    Group context (``group_id``, ``role``, ``group_tier``, ``member_count``) is
    a snapshot taken when it was sent, so later changes don't rewrite it.

    ``status``: new -> publishing -> published, or dismissed. ``publishing``
    is the claim a Publish click takes before posting, so two clicks can never
    post the quote twice.
    """

    __tablename__ = "feedback_responses"
    __table_args__ = (
        Index("idx_feedback_response_created", "created_at"),
        Index("idx_feedback_response_user", "user_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(Integer, ForeignKey("feedback_requests.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(Integer, nullable=False)
    group_id = Column(Integer, nullable=True)
    role = Column(String(12), nullable=True)  # owner|admin
    group_tier = Column(String(32), nullable=True)
    member_count = Column(Integer, nullable=True)
    rating = Column(SmallInteger, nullable=False)  # 1-10
    liked_text = Column(Text, nullable=True)
    improve_text = Column(Text, nullable=True)
    quote_consent = Column(Boolean, nullable=False, default=False)
    attribution = Column(String(8), nullable=False, default="clan")  # clan|full|anon
    contact_ok = Column(Boolean, nullable=False, default=False)
    status = Column(String(12), nullable=False, default="new")  # new|publishing|published|dismissed
    raw_message_id = Column(String(32), nullable=True)
    published_message_id = Column(String(32), nullable=True)
    published_by = Column(Integer, nullable=True)
    published_at = Column(DateTime, nullable=True)
    reviewed_by = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
