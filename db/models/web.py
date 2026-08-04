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


class GroupEventManager(Base):
    """Group-scoped 'event manager' grant (web64a): a member the group's admins
    trust to fully manage the group's EVENTS — create/edit/delete events and
    their tasks/teams/board/prizes/Discord — WITHOUT any group-admin access
    (settings, members, subscription, config, appointing others).

    Deliberately a SEPARATE table from ``group_admins`` rather than a new
    ``role`` value: ``resolve_group_role`` collapses non-owner grants to
    ``admin`` and dozens of surfaces trust ``role in ('owner','admin')`` for
    full admin, so a stray enum value would leak admin rights. The event gates
    (``web_api/deps.is_event_manager`` + ``_assert_event_admin``) consult this
    table narrowly; ``assert_group_admin`` never does. Grants are web-only —
    the Discord bot's ``authed_users`` list is untouched.
    """

    __tablename__ = "group_event_managers"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uix_group_event_manager"),
        Index("idx_group_event_manager_group", "group_id"),
        Index("idx_group_event_manager_user", "user_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
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
    # message|announcement|forum_post|delete_message
    kind = Column(String(24), nullable=False, default="message")
    channel_id = Column(String(32), nullable=False)
    content = Column(Text, nullable=True)
    embed_json = Column(Text, nullable=True)
    ref_type = Column(String(24), nullable=True)   # e.g. 'announcement'
    ref_id = Column(Integer, nullable=True)
    # pending|sending|sent|failed. `sending` is a claim taken before the row is
    # handed to Discord, so a crash mid-drain leaves evidence instead of a row
    # that looks untouched and gets posted a second time.
    status = Column(String(16), nullable=False, default="pending")
    # Write-back for sent messages; for kind='delete_message' it is instead
    # supplied up front and names the message to remove.
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
        # Event-scoped audit browser (web57a): the manager audit log filters one
        # event's admin actions by this column, newest-first.
        Index("idx_audit_event_created", "event_id", "created_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    # The web event this action happened inside, when it is event-scoped
    # (web57a). Deliberately NOT a ForeignKey: an event hard-delete must not
    # be blocked by — nor cascade away — its own durable audit trail, and
    # library/template/config actions legitimately carry NULL here.
    event_id = Column(Integer, nullable=True)
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


class Suggestion(Base):
    """Suggestion / bug-report threads, mirrored 1:1 with Discord forum posts.

    Threads start on either side. Web-origin rows are written by the Web API
    (§10.2: it never talks to Discord) and syndicated by the core bot as a
    forum post via a ``kind='forum_post'`` outbox row, which writes back the
    thread id and flips ``status`` to 'posted'. Discord-origin rows are
    created by the webhook bot's mirror (services/suggestion_sync.py) when a
    post appears directly in a tracked forum. Replies live in
    ``suggestion_messages``.
    """

    __tablename__ = "suggestions"
    __table_args__ = (
        Index("idx_suggestion_user_created", "user_id", "created_at"),
        Index("idx_suggestion_activity", "last_activity_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Site account of the author; NULL for Discord-origin threads whose
    # author has no linked DropTracker account.
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    origin = Column(String(8), nullable=False, default="web")  # web|discord
    # Author snapshot for Discord-origin threads (display without a join).
    author_discord_id = Column(String(35), nullable=True)
    author_name = Column(String(100), nullable=True)
    type = Column(String(16), nullable=False, default="suggestion")  # suggestion|bug
    # Doubles as the Discord thread name (Discord caps thread names at 100).
    title = Column(String(100), nullable=False)
    body_md = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending")  # pending|posted|failed
    # False once the Discord thread is archived, locked, or deleted.
    is_open = Column(Boolean, nullable=False, default=True)
    message_count = Column(Integer, nullable=False, default=0)
    discord_thread_id = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    last_activity_at = Column(DateTime, default=func.now(), nullable=False)


class SuggestionMessage(Base):
    """One reply on a suggestion thread, from either side of the mirror.

    ``discord_message_id`` keys the two-way sync: Discord-origin rows store
    the mirrored message's id (idempotent upserts, edit/delete tracking), and
    web-origin rows get it written back by the outbox drain after the bot
    relays them, so the mirror never re-imports the bot's relay as a new
    reply (it skips bot authors anyway).
    """

    __tablename__ = "suggestion_messages"
    __table_args__ = (
        Index("idx_suggestion_message_thread", "suggestion_id", "created_at"),
        UniqueConstraint("discord_message_id", name="uix_suggestion_message_discord"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    suggestion_id = Column(Integer, ForeignKey("suggestions.id"), nullable=False)
    author_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    author_discord_id = Column(String(35), nullable=True)
    author_name = Column(String(100), nullable=False)
    source = Column(String(8), nullable=False)  # web|discord
    content = Column(Text, nullable=False)
    discord_message_id = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    edited_at = Column(DateTime, nullable=True)


class SiteRedirect(Base):
    """Admin-configurable URL redirects, resolved at request time by the
    front-end's Next.js middleware — no code deploy is needed to add or change
    one, which is the whole point of the feature. Same DB-table-as-CMS pattern
    as ``DocsPage``: edited through ``/admin/redirects``.

    ``source`` is a path-to-regexp pattern using the same syntax as the static
    map in the front-end's ``next.config.ts`` (e.g. ``/players/view/:id(\\d+)``).
    ``destination`` is either an internal path (``/docs``) or an absolute URL
    (``https://runelite.net``); ``:param`` tokens captured from the source are
    substituted into it. ``permanent`` selects the HTTP status: 308 when true,
    307 when false. Entries are evaluated in ``order`` (ascending); first match
    wins.
    """

    __tablename__ = "site_redirects"
    __table_args__ = (
        Index("idx_site_redirect_order", "order", "id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(512), nullable=False, unique=True)
    destination = Column(String(1024), nullable=False)
    permanent = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    order = Column(Integer, nullable=False, default=100)
    forward_query = Column(Boolean, nullable=False, default=True)
    note = Column(String(255), nullable=True)
    author_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
