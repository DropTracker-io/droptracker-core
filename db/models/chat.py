"""Generic threaded messaging (web96a).

A reusable conversation substrate. The first — and so far only — surface is the
clan-vs-clan challenge negotiation (``services/chat`` +
``web_api/routes/chat.py``), but nothing here knows about events: a thread is
anchored to an arbitrary ``(subject_type, subject_id)`` pair, so support
threads, recruitment DMs or split disputes attach later without a migration.

**A participant is a party, not a user.** ``chat_participants`` rows name a
*group* or a *user*, and for a group party ANY of that clan's authorized admins
may read and speak as the clan. That is the whole reason this exists rather
than a user-to-user DM table: a challenge is sent to a clan, and whichever of
its leaders happens to log in first must be able to answer it. Which humans a
group party resolves to is deliberately NOT stored — it is derived live in
``services.chat.resolve_membership`` from ``resolve_group_role`` +
``group_event_managers``, so a leadership change takes effect immediately and
a removed admin loses access without a backfill.

Message bodies are plain text (rendered, never interpreted as HTML) and
attachments are references to already-uploaded B2 objects, so this table never
holds bytes. There is intentionally no author edit/delete in v1; the only
takedown path is a moderator tombstone (``deleted_at``/``deleted_by_user_id``),
which keeps the row so a negotiation can't be silently rewritten after the
fact.
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
    UniqueConstraint,
    func,
)

from .base import Base

#: Thread kinds. Only ``event_invite`` is wired today; the column is a plain
#: string so a new surface adds a value here and nothing else.
CHAT_THREAD_KINDS = ("event_invite",)

#: ``open`` accepts new messages; ``locked`` is readable but closed to posts
#: (e.g. an event that ended); ``archived`` is hidden from the default list.
CHAT_THREAD_STATUSES = ("open", "locked", "archived")

#: Who a participant/author is. A ``group`` party is spoken for by its
#: authorized admins; a ``user`` party is one person.
CHAT_PARTY_TYPES = ("group", "user")

#: ``message`` is human-written; ``system`` is a generated timeline entry
#: (invite sent/accepted/declined) with no author and a typed ``system_code``.
CHAT_MESSAGE_KINDS = ("message", "system")

#: Hard cap on a single message body, enforced by the route. Matches Discord's
#: limit so a body is always relayable if a future surface wants to mirror it.
CHAT_BODY_MAX_CHARS = 2000

#: Attachments per message. Small on purpose — this is a negotiation thread,
#: not a gallery.
CHAT_MAX_ATTACHMENTS = 4


class ChatThread(Base):
    """One conversation, anchored to some subject in the rest of the app."""

    __tablename__ = "chat_threads"
    __table_args__ = (
        # Makes get-or-create race-safe: two admins bulk-inviting the same clan
        # at once both try to insert, one loses on the unique key and re-reads.
        UniqueConstraint(
            "kind", "subject_type", "subject_id", name="uq_chat_thread_subject"
        ),
        Index("idx_chat_thread_activity", "status", "last_message_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(32), nullable=False)
    # Polymorphic anchor. For clan-vs-clan invites: ('event_group', <
    # web_event_groups.id>) — one thread per (event, invited clan), so a
    # 12-clan event holds 12 private negotiations rather than one shared room.
    subject_type = Column(String(32), nullable=False)
    subject_id = Column(Integer, nullable=False)
    title = Column(String(255), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    status = Column(String(16), nullable=False, default="open")
    # Denormalised from the newest message so the thread list sorts and renders
    # without touching chat_messages. NULL until the first message lands.
    last_message_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())


class ChatParticipant(Base):
    """A party on a thread. See the module docstring: group parties resolve to
    humans live, never through a stored roster."""

    __tablename__ = "chat_participants"
    __table_args__ = (
        UniqueConstraint(
            "thread_id", "party_type", "party_id", name="uq_chat_participant"
        ),
        # "Which threads can this party see?" — the thread-list query.
        Index("idx_chat_participant_party", "party_type", "party_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(
        Integer, ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    party_type = Column(String(16), nullable=False)
    # NOT a foreign key: the referent depends on party_type (groups.group_id or
    # users.user_id), and MySQL has no polymorphic FK. Writers validate.
    party_id = Column(Integer, nullable=False)
    # 'owner' is the party that opened the thread (the challenger, here) —
    # display only; it grants no extra rights.
    role = Column(String(16), nullable=False, default="member")
    added_at = Column(DateTime, nullable=False, default=func.now())


class ChatMessage(Base):
    """One entry in a thread's timeline — human message or system event."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        # The only read pattern: a page of a thread, newest-id-descending.
        Index("idx_chat_message_thread", "thread_id", "id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(
        Integer, ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    kind = Column(String(16), nullable=False, default="message")
    # NULL for system entries. Kept alongside the party because "who typed it"
    # and "which clan they spoke for" are different questions — an admin can
    # legitimately hold rights on both clans in a battle.
    author_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    author_party_type = Column(String(16), nullable=True)
    author_party_id = Column(Integer, nullable=True)
    # Plain text. Never rendered as HTML/markdown-with-embeds by the client.
    body = Column(Text, nullable=True)
    #: JSON list of {url, key, content_type} for already-uploaded B2 objects.
    attachments_json = Column(Text, nullable=True)
    # Typed system entry, e.g. 'invite_sent'. The client owns the wording so
    # copy changes don't need a backfill; system_data_json carries the nouns.
    system_code = Column(String(32), nullable=True)
    system_data_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    # Moderator tombstone. The row survives so the timeline keeps its shape and
    # an audit can still see that something was said and removed.
    deleted_at = Column(DateTime, nullable=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)


class ChatRead(Base):
    """Per-user read pointer, driving unread counts.

    Per USER, not per party: two admins of the same clan read independently,
    and a badge that cleared because a co-leader opened the thread would be
    worse than no badge at all.
    """

    __tablename__ = "chat_reads"
    __table_args__ = ({"extend_existing": True},)

    thread_id = Column(
        Integer,
        ForeignKey("chat_threads.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = Column(Integer, ForeignKey("users.user_id"), primary_key=True)
    # Highest message id this user has seen. Advance-only — see
    # services.chat.mark_read, which never moves it backwards.
    last_read_message_id = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )
