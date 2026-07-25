"""Badge system ORM models (web platform).

Badges are awards players earn automatically (daily loot champion, loot
streaks, boss record holder) or manually (superadmin grants from /admin).

Two tables:
    Badge       - the catalog/definition of a badge type.
    PlayerBadge - individual awards, with full history retained.

Award semantics (``Badge.semantic``):
    'permanent' - once earned, kept forever (e.g. daily champion for a date).
    'held'      - one current holder per slot; when beaten the old award is
                  marked ``lost`` (kept as history) and a new active row is
                  inserted.

Dedupe design: the unique index on ``(badge_id, group_key, active_key)``
enforces exactly one *active* award per badge+slot. ``active_key`` mirrors
``slot_key`` while ``status='active'`` and is NULLed when the award is lost or
revoked — MySQL treats NULLs as distinct in unique indexes, so history rows
never collide. ``group_key`` is ``group_id or 0`` because a nullable column
cannot take part in the dedupe (NULL group_id = global badge).

``slot_key`` encodes the uniqueness domain per badge type:
    daily champion  -> the day token ("20260704"): one winner per day
    streak / manual -> "p:{player_id}": once per player (revoke frees it)
    boss record     -> "npc:{npc_id}:{team_size}": one active holder per slot
    loot leader     -> the partition token ("all" / "202607"): one leader per
                       board — held (live crown) or permanent (month-end
                       trophy), per this row's ``semantic``
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base

BADGE_SEMANTICS = ("permanent", "held")
BADGE_STATUSES = ("active", "lost", "revoked")
# Must stay in lockstep with BADGE_TONES in apps/web/components/ui.tsx.
BADGE_TONES = ("gold", "green", "red", "neutral", "purple", "sky", "ember", "bronze")


class Badge(Base):
    """Catalog of badge definitions (rows are admin-editable; automatic
    behavior is code-owned — ``criteria`` JSON's ``type`` maps to a Python
    evaluator in ``services/badges.py``, NULL criteria = manual-only)."""

    __tablename__ = "badges"
    __table_args__ = (
        {"extend_existing": True},
    )

    badge_id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    description = Column(String(300), nullable=False)
    icon_url = Column(String(512), nullable=True)
    icon_emoji = Column(String(16), nullable=True)
    tone = Column(String(16), nullable=False, default="gold")  # BADGE_TONES
    semantic = Column(String(16), nullable=False, default="permanent")  # BADGE_SEMANTICS
    scope = Column(String(16), nullable=False, default="global")  # 'global' | 'group'
    active = Column(Boolean, nullable=False, default=True)  # soft delete
    criteria = Column(Text, nullable=True)  # JSON; NULL = manual-only badge
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    awards = relationship("PlayerBadge", back_populates="badge")


class PlayerBadge(Base):
    """An individual badge award. Rows are never deleted: lost/revoked awards
    keep the history visible on profiles ("Held until ...")."""

    __tablename__ = "player_badges"
    __table_args__ = (
        Index("ux_player_badges_active", "badge_id", "group_key", "active_key", unique=True),
        Index("idx_player_badges_player_status", "player_id", "status"),
        Index("idx_player_badges_badge_status", "badge_id", "status"),
        {"extend_existing": True},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    badge_id = Column(Integer, ForeignKey("badges.badge_id"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)  # NULL = global
    group_key = Column(Integer, nullable=False, default=0)  # group_id or 0; see module docstring
    status = Column(String(16), nullable=False, default="active")  # BADGE_STATUSES
    slot_key = Column(String(64), nullable=False, default="")
    active_key = Column(String(64), nullable=True)  # = slot_key while active, NULL otherwise
    awarded_at = Column(DateTime, default=func.now(), nullable=False)
    lost_at = Column(DateTime, nullable=True)
    awarded_by = Column(Integer, ForeignKey("users.user_id"), nullable=True)  # manual awards only
    context = Column(Text, nullable=True)  # JSON, e.g. {"day":"20260704","loot":1234567}

    badge = relationship("Badge", back_populates="awards")
    player = relationship("Player")
