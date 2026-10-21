"""Customizable public page layouts (web137a).

One row per (scope, subject): a group's public page, a player's own profile,
or a group's default layout for its members' profiles. The builder only ever
writes ``draft_json``. Publishing copies it to ``published_json``, the only
column the public pages read. Whether a published layout is actually shown
is decided at read time by the owner's entitlement, so a lapsed subscription
falls back to the standard page without losing the saved design.

Structure and limits live in ``web_api/page_layouts_shared.py``.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT

from .base import Base

# scope -> what subject_id refers to
LAYOUT_SCOPES = ("group", "player", "member_default")


class PageLayout(Base):
    __tablename__ = "page_layouts"
    __table_args__ = (
        UniqueConstraint("scope", "subject_id", name="uq_page_layouts_scope_subject"),
        {"extend_existing": True},
    )

    layout_id = Column(Integer, primary_key=True, autoincrement=True)
    # "group" and "member_default" -> groups.group_id; "player" -> players.player_id.
    scope = Column(String(16), nullable=False)
    subject_id = Column(Integer, nullable=False)
    draft_json = Column(LONGTEXT, nullable=False)
    published_json = Column(LONGTEXT, nullable=True)
    updated_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    published_at = Column(DateTime, nullable=True)
