"""Device tokens for the owner's Android admin widget.

A superadmin pairs a phone from /admin/widget; the phone then sends
``Authorization: Bearer dtw_...`` to exactly one read-only endpoint,
``GET /api/v1/admin/widget/summary`` (web_api/routes/admin_widget.py). The token
is not a session: nothing else accepts it, so a lost phone can read the
summary until the token is revoked, and do nothing more.

Only a SHA-256 of the token is stored. The raw value is shown once, at pairing.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, func

from .base import Base


class AdminWidgetToken(Base):
    __tablename__ = "admin_widget_tokens"
    __table_args__ = (
        Index("ux_awt_token_hash", "token_hash", unique=True),
        Index("idx_awt_user", "user_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    label = Column(String(64), nullable=False)
    token_hash = Column(String(64), nullable=False)
    # First characters of the raw token, so the list can tell devices apart.
    token_hint = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now())
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
