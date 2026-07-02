"""Web-platform ORM models (FRONTEND_PLAN.md §13, backend Task 08).

These tables back the first-party Next.js front-end and are owned by the Web
API v1. They live in the ``data`` database alongside the existing models but are
only written by the web surface (and, for syndication, the Discord bot).

Contains:
    GroupAdmin   - explicit web-granted admin rights on a group (§7.2 roles).
    Announcement - the announcements feature (§10); web pages are canonical.
    AuditLog     - audit trail for config/admin actions (§11, §15).

Session/OAuth-state tables from the Task 08 spec are intentionally omitted: the
Web API uses stateless JWT sessions (see ``web_api/session.py``) with a Redis
deny-list for revocation, so no server-side session table is required.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base


class GroupAdmin(Base):
    """Explicit web-granted admin rights on a group, beyond owner/MANAGE_GUILD.

    Role derivation (§7.2) still treats the group's Discord guild MANAGE_GUILD
    holders and the original creator as owners; this table records grants made
    from the website and seeds existing owners on rollout.
    """

    __tablename__ = "group_admins"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uix_group_admin"),
        Index("idx_group_admin_group", "group_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    role = Column(String(16), nullable=False, default="admin")  # 'owner' | 'admin'
    granted_by = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class Announcement(Base):
    """Announcements feature (§10). Web pages are the canonical, SEO-indexed
    source; Discord is a syndication target."""

    __tablename__ = "announcements"
    __table_args__ = (
        Index("idx_announcement_scope", "scope_type", "group_id", "published_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_type = Column(String(16), nullable=False, default="group")  # 'global' | 'group'
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    author_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    title = Column(String(200), nullable=False)
    body_md = Column(Text, nullable=False)
    cover_image_url = Column(String(512), nullable=True)
    pinned = Column(Boolean, default=False, nullable=False)
    status = Column(String(16), default="draft", nullable=False)  # draft|published|archived
    published_at = Column(DateTime, nullable=True)
    # Discord syndication refs (§10.1/§10.2), written back by the bot.
    discord_message_id = Column(String(32), nullable=True)
    discord_channel_id = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class DiscordOutbox(Base):
    """Queue of Discord messages for the bot to send (Tasks 09 + 12).

    The Web API must never open a Discord connection (§10.2). Instead it inserts
    a row here; the Discord bot drains it (see ``services/discord_outbox.py``)
    and, for announcements, writes back ``discord_message_id`` so later
    edits/deletes can sync.
    """

    __tablename__ = "discord_outbox"
    __table_args__ = (
        Index("idx_outbox_status_created", "status", "created_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(24), nullable=False, default="message")   # message|announcement
    channel_id = Column(String(32), nullable=False)
    content = Column(Text, nullable=True)
    embed_json = Column(Text, nullable=True)
    ref_type = Column(String(24), nullable=True)   # e.g. 'announcement'
    ref_id = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False, default="pending")  # pending|sent|failed
    discord_message_id = Column(String(32), nullable=True)
    error = Column(Text, nullable=True)
    actor_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    processed_at = Column(DateTime, nullable=True)


class AuditLog(Base):
    """Audit trail for config/admin actions (Task 05, §11, §15)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_group_created", "group_id", "created_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    action = Column(String(64), nullable=False)      # e.g. 'config.update'
    target = Column(String(128), nullable=True)      # e.g. 'group_configurations.notify_pets'
    before = Column(Text, nullable=True)
    after = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class DocsPage(Base):
    """User-editable docs pages (superadmin CMS). Replaces the original static
    `.mdx` files under `apps/web/content/docs/` so docs can be added, edited,
    and deleted from `/admin/docs` without a code deploy. `body_md` is plain
    Markdown (not MDX) — rendered via the safe `react-markdown` pipeline on the
    frontend, deliberately not the JSX-executing MDX compiler, since this
    content is now editable through a web form rather than authored in a repo
    under code review."""

    __tablename__ = "docs_pages"
    __table_args__ = (
        Index("idx_docs_page_category_order", "category", "order"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(120), nullable=False, unique=True)
    title = Column(String(200), nullable=False)
    description = Column(String(300), nullable=True)
    category = Column(String(80), nullable=False, default="General")
    order = Column(Integer, nullable=False, default=100)
    body_md = Column(Text, nullable=False)
    author_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
