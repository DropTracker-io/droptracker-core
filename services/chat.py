"""Threaded messaging domain layer (web96a).

Everything the chat subsystem *decides* lives here: who may read a thread, who
may speak on it and as which party, what a message body may contain, and what
the read pointer means. The HTTP layer (``web_api/routes/chat.py``) does
nothing but shape requests into these calls, and the CvC invite flow
(``web_api/routes/event_participants.py``) uses the same functions — so there
is exactly one place where "may this person speak for that clan?" is answered.

**Group parties resolve to humans live.** A ``chat_participants`` row names a
clan, never a roster of user ids: :func:`resolve_membership` re-derives the
answer on every call from ``resolve_group_role`` plus ``group_event_managers``.
A promoted admin can reply immediately and a demoted one loses access on their
next request, with no backfill and no stale copy to drift.

Module-level imports are stdlib-only on purpose (same contract as
``services/event_signup.py``): DB models, Redis and ``web_api`` helpers are
imported inside functions so unit tests can load this module for real under
conftest's stubbed ``db``/``services`` packages.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

# Kept in step with db/models/chat.py, but defined here so route/service code
# validating against them never depends on the DB package (which unit tests
# replace with a MagicMock — a tuple membership test against one of those
# silently passes and the validation quietly evaporates).
THREAD_KINDS = ("event_invite", "staff_dm", "group_notice")
THREAD_STATUSES = ("open", "locked", "archived")
PARTY_TYPES = ("group", "user")
MESSAGE_KINDS = ("message", "system")

BODY_MAX_CHARS = 2000
MAX_ATTACHMENTS = 4

#: Typed timeline entries. The *client* owns the wording — we store the code
#: plus the nouns it needs, so rephrasing "X declined your challenge" never
#: requires rewriting rows.
SYSTEM_CODES = (
    "invite_sent",
    "invite_accepted",
    "invite_declined",
    "invite_withdrawn",
    "event_activated",
    "event_ended",
    # staff_dm (web102a)
    "staff_dm_opened",
    "dm_bounced",
    # group_notice (web102a)
    "notice_raised",
    "notice_recurred",
    "notice_resolved",
)

#: Default page size for a thread's message history.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Party:
    """A side of a conversation: a clan (``group``) or a person (``user``)."""

    type: str
    id: int

    def as_tuple(self) -> tuple[str, int]:
        return (self.type, self.id)


@dataclass(frozen=True)
class Membership:
    """What a given user may do on a given thread.

    ``parties`` is a tuple rather than a single value because a person can
    legitimately hold admin rights on BOTH clans in a battle. Rather than
    silently picking one for them, we hand the caller everything they qualify
    for and let the UI ask which hat they are wearing.
    """

    parties: tuple[Party, ...]
    can_post: bool
    is_moderator: bool = False

    @property
    def primary(self) -> Optional[Party]:
        return self.parties[0] if self.parties else None

    def allows(self, party: Optional[Party]) -> bool:
        """Whether this membership may post AS ``party`` (None = the default)."""
        if party is None:
            return bool(self.parties)
        return party in self.parties


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #
def get_or_create_thread(
    s,
    *,
    kind: str,
    subject_type: str,
    subject_id: int,
    parties: Sequence[tuple[str, int]],
    title: Optional[str] = None,
    created_by_user_id: Optional[int] = None,
    owner_party: Optional[tuple[str, int]] = None,
    commit: bool = True,
):
    """Return the thread for ``(kind, subject_type, subject_id)``, creating it
    and its participant rows if absent.

    Idempotent and race-safe: the unique key on the subject triple means a
    concurrent creator loses on flush, and we re-read theirs rather than
    ending up with two threads for one negotiation.
    """
    from db.models import ChatParticipant, ChatThread

    existing = (
        s.query(ChatThread)
        .filter(
            ChatThread.kind == kind,
            ChatThread.subject_type == subject_type,
            ChatThread.subject_id == subject_id,
        )
        .first()
    )
    if existing is not None:
        _ensure_participants(s, existing.id, parties, owner_party)
        if commit:
            s.commit()
        return existing

    thread = ChatThread(
        kind=kind,
        subject_type=subject_type,
        subject_id=int(subject_id),
        title=(title or None),
        created_by_user_id=created_by_user_id,
        status="open",
    )
    s.add(thread)
    try:
        s.flush()
    except Exception:
        # Someone else created it between our SELECT and this INSERT.
        s.rollback()
        thread = (
            s.query(ChatThread)
            .filter(
                ChatThread.kind == kind,
                ChatThread.subject_type == subject_type,
                ChatThread.subject_id == subject_id,
            )
            .first()
        )
        if thread is None:
            raise
        _ensure_participants(s, thread.id, parties, owner_party)
        if commit:
            s.commit()
        return thread

    for party_type, party_id in parties:
        s.add(
            ChatParticipant(
                thread_id=thread.id,
                party_type=party_type,
                party_id=int(party_id),
                role=(
                    "owner"
                    if owner_party is not None
                    and (party_type, int(party_id)) == (owner_party[0], int(owner_party[1]))
                    else "member"
                ),
            )
        )
    if commit:
        s.commit()
    return thread


def _ensure_participants(
    s,
    thread_id: int,
    parties: Sequence[tuple[str, int]],
    owner_party: Optional[tuple[str, int]] = None,
) -> None:
    """Add any of ``parties`` missing from an existing thread. Never removes —
    a party that has seen the conversation keeps its access."""
    from db.models import ChatParticipant

    have = {
        (p.party_type, p.party_id)
        for p in s.query(ChatParticipant)
        .filter(ChatParticipant.thread_id == thread_id)
        .all()
    }
    for party_type, party_id in parties:
        if (party_type, int(party_id)) in have:
            continue
        s.add(
            ChatParticipant(
                thread_id=thread_id,
                party_type=party_type,
                party_id=int(party_id),
                role=(
                    "owner"
                    if owner_party is not None
                    and (party_type, int(party_id)) == (owner_party[0], int(owner_party[1]))
                    else "member"
                ),
            )
        )


def thread_by_subject(s, kind: str, subject_type: str, subject_id: int):
    from db.models import ChatThread

    return (
        s.query(ChatThread)
        .filter(
            ChatThread.kind == kind,
            ChatThread.subject_type == subject_type,
            ChatThread.subject_id == int(subject_id),
        )
        .first()
    )


def set_thread_status(s, thread, status: str, *, commit: bool = True) -> None:
    """Lock/archive/reopen a thread. Unknown statuses are ignored rather than
    raising — a caller reacting to some other subsystem's state change should
    never be able to 500 it."""
    if status not in THREAD_STATUSES:
        return
    thread.status = status
    if commit:
        s.commit()


# --------------------------------------------------------------------------- #
# Authorization — the single funnel
# --------------------------------------------------------------------------- #
def speakable_group_ids(s, user_id: int) -> set[int]:
    """Group ids ``user_id`` may speak for: owner/admin (explicit grant,
    membership or MANAGE_GUILD) OR a ``group_event_managers`` grant.

    Deliberately does NOT special-case superadmins into "every group on the
    site": that would flood a staff member's thread list with every clan's
    private negotiations. Staff still read and moderate any individual thread —
    :func:`resolve_membership` grants that per-thread, because
    ``resolve_group_role`` already returns 'owner' for them.
    """
    if user_id is None:
        return set()
    from db.models import Group, GroupAdmin
    from web_api.deps import (
        event_manager_group_ids,
        load_user,
        manageable_guild_ids,
        resolve_group_role,
    )

    user = load_user(s, user_id)
    mgids = manageable_guild_ids(user_id)
    candidates: set[int] = set()
    if user is not None:
        candidates |= {g.group_id for g in (user.groups or [])}
    candidates |= {
        gid
        for (gid,) in s.query(GroupAdmin.group_id)
        .filter(GroupAdmin.user_id == user_id)
        .all()
    }
    if mgids:
        candidates |= {
            gid
            for (gid,) in s.query(Group.group_id).filter(Group.guild_id.in_(mgids)).all()
        }

    out = {
        gid
        for gid in candidates
        if resolve_group_role(s, user_id, gid, mgids, user=user) in ("owner", "admin")
    }
    # Event managers speak for their clan on event threads without holding any
    # group-admin right (web64a's whole point), so they are unioned in rather
    # than filtered through the role check above, which would drop them.
    out |= {int(gid) for gid in event_manager_group_ids(s, user_id)}
    return out


def resolve_membership(s, thread, user_id: int) -> Optional[Membership]:
    """Every party ``user_id`` may speak as on ``thread``, or None if they may
    not even read it. Fails closed on anything unexpected.

    A ``user`` party matches by identity; a ``group`` party matches when the
    caller is owner/admin of that clan or holds an event-manager grant on it.
    """
    if thread is None or user_id is None:
        return None
    from db.models import ChatParticipant
    from web_api.deps import (
        is_event_manager,
        is_superadmin,
        load_user,
        manageable_guild_ids,
        resolve_group_role,
    )

    participants = (
        s.query(ChatParticipant)
        .filter(ChatParticipant.thread_id == thread.id)
        .order_by(ChatParticipant.id.asc())
        .all()
    )
    if not participants:
        return None

    user = load_user(s, user_id)
    moderator = bool(is_superadmin(user))
    mgids = manageable_guild_ids(user_id)

    matched: list[Party] = []
    for p in participants:
        if p.party_type == "user":
            if int(p.party_id) == int(user_id):
                matched.append(Party("user", int(p.party_id)))
            continue
        if p.party_type == "group":
            gid = int(p.party_id)
            role = resolve_group_role(s, user_id, gid, mgids, user=user)
            if role in ("owner", "admin") or is_event_manager(s, user_id, gid):
                matched.append(Party("group", gid))

    if not matched:
        # Support staff hold a seat on every thread kind without a stored
        # participant row (web102a): any developer/superadmin may read and
        # answer a staff_dm, a group_notice, or a clan-vs-clan negotiation,
        # exactly as any of a clan's admins may answer for the clan. Staff
        # mediate CvC disputes, so being locked out of the conversation they
        # are asked to arbitrate was the wrong default.
        #
        # This widens who may open a thread BY ID; it deliberately does not
        # widen the thread LIST. `speakable_group_ids` still refuses to fold
        # every clan on the site into a staff member's own inbox — the staff
        # browse routes query by kind instead, so a negotiation reaches staff
        # eyes only when they go looking for it.
        if getattr(thread, "kind", None) in THREAD_KINDS:
            from web_api.deps import is_support_staff

            if is_support_staff(user):
                return Membership(
                    parties=(Party("user", int(user_id)),),
                    can_post=(thread.status == "open"),
                    is_moderator=moderator,
                )
        return None
    return Membership(
        parties=tuple(matched),
        can_post=(thread.status == "open"),
        is_moderator=moderator,
    )


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
class ChatError(ValueError):
    """Domain rejection with a user-facing message (the route maps it to 422)."""


def normalize_body(body: Optional[str]) -> str:
    """Trim and validate a human message body. Empty is only legal when the
    message carries attachments — the caller checks that."""
    text = (body or "").strip()
    if len(text) > BODY_MAX_CHARS:
        raise ChatError(f"Messages are capped at {BODY_MAX_CHARS} characters.")
    return text


def normalize_attachments(raw: Any) -> list[dict]:
    """Validate client-supplied attachment references.

    The client uploads through ``POST /api/v1/uploads/proof`` and hands us back
    the object key. We keep the KEY and re-derive the public URL ourselves —
    trusting a client-supplied URL would let anyone post an arbitrary remote
    image into a thread under our chrome.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ChatError("'attachments' must be a list.")
    if len(raw) > MAX_ATTACHMENTS:
        raise ChatError(f"At most {MAX_ATTACHMENTS} attachments per message.")

    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ChatError("Each attachment must be an object with a 'key'.")
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ChatError("Each attachment needs an upload 'key'.")
        key = key.strip().lstrip("/")
        # The upload route mints keys under this prefix; anything else was not
        # produced by us and must not be rendered as though it were.
        if not key.startswith("dt_uploads/") or ".." in key:
            raise ChatError("That attachment key was not issued by an upload.")
        out.append({"key": key, "url": attachment_url(key)})
    return out


def attachment_url(key: str) -> str:
    """Public CDN URL for an upload key. Falls back to a root-relative path so
    a missing CDN config degrades to a broken image, not a crash."""
    try:
        from utils.b2_storage import B2_CDN_BASE_URL

        base = (B2_CDN_BASE_URL or "").rstrip("/")
    except Exception:
        base = ""
    return f"{base}/{key}" if base else f"/{key}"


def post_message(
    s,
    *,
    thread,
    author_user_id: int,
    party: Party,
    body: Optional[str],
    attachments: Any = None,
    source: str = "web",
    discord_message_id: Optional[str] = None,
    commit: bool = True,
    publish: bool = True,
):
    """Append a human message. Raises :class:`ChatError` on invalid input.

    ``source``/``discord_message_id`` are the Discord-bridge columns
    (web102a): the staff-DM bridge ingests with ``source='discord_dm'`` plus
    the DM's message id, which the unique key turns into redelivery
    idempotency.
    """
    from db.models import ChatMessage

    if thread.status != "open":
        raise ChatError("This conversation is closed to new messages.")
    text = normalize_body(body)
    files = normalize_attachments(attachments)
    if not text and not files:
        raise ChatError("Write something or attach a file.")

    row = ChatMessage(
        thread_id=thread.id,
        kind="message",
        author_user_id=author_user_id,
        author_party_type=party.type,
        author_party_id=int(party.id),
        body=text or None,
        attachments_json=json.dumps(files) if files else None,
        source=source,
        discord_message_id=(str(discord_message_id) if discord_message_id else None),
    )
    s.add(row)
    thread.last_message_at = datetime.now()
    if commit:
        s.commit()
        if publish:
            publish_message(s, thread, row)
    return row


def post_system(
    s,
    *,
    thread,
    code: str,
    data: Optional[dict] = None,
    actor_user_id: Optional[int] = None,
    party: Optional[Party] = None,
    commit: bool = True,
    publish: bool = True,
):
    """Append a typed timeline entry (invite sent/accepted/declined...).

    Recorded with its actor so "unread messages that aren't mine" stays true
    for system entries too — accepting your own invite must not light up your
    own badge.
    """
    from db.models import ChatMessage

    if code not in SYSTEM_CODES:
        raise ChatError(f"Unknown system code {code!r}.")
    row = ChatMessage(
        thread_id=thread.id,
        kind="system",
        author_user_id=actor_user_id,
        author_party_type=party.type if party else None,
        author_party_id=int(party.id) if party else None,
        system_code=code,
        system_data_json=json.dumps(data) if data else None,
    )
    s.add(row)
    thread.last_message_at = datetime.now()
    if commit:
        s.commit()
        if publish:
            publish_message(s, thread, row)
    return row


def soft_delete_message(s, message, *, by_user_id: int, commit: bool = True) -> None:
    """Moderator tombstone. The row stays so the timeline keeps its shape and
    an audit can still see that something was said and taken down."""
    if message.deleted_at is not None:
        return
    message.deleted_at = datetime.now()
    message.deleted_by_user_id = by_user_id
    if commit:
        s.commit()


def messages_page(
    s,
    thread_id: int,
    *,
    before_id: Optional[int] = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list:
    """One page of a thread, OLDEST-first for rendering. ``before_id`` pages
    backwards through history."""
    from db.models import ChatMessage

    limit = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    q = s.query(ChatMessage).filter(ChatMessage.thread_id == thread_id)
    if before_id:
        q = q.filter(ChatMessage.id < int(before_id))
    rows = q.order_by(ChatMessage.id.desc()).limit(limit).all()
    return list(reversed(rows))


# --------------------------------------------------------------------------- #
# Read state
# --------------------------------------------------------------------------- #
def mark_read(s, thread_id: int, user_id: int, message_id: int, *, commit: bool = True):
    """Advance the user's read pointer. Never moves backwards — a stale tab
    reporting an old id must not resurrect a badge the user already cleared."""
    from db.models import ChatRead

    row = (
        s.query(ChatRead)
        .filter(ChatRead.thread_id == thread_id, ChatRead.user_id == user_id)
        .first()
    )
    target = max(0, int(message_id or 0))
    if row is None:
        row = ChatRead(
            thread_id=thread_id, user_id=user_id, last_read_message_id=target
        )
        s.add(row)
    elif target > (row.last_read_message_id or 0):
        row.last_read_message_id = target
        row.updated_at = datetime.now()
    if commit:
        s.commit()
    return row


def unread_counts(s, thread_ids: Iterable[int], user_id: int) -> dict[int, int]:
    """Unread message count per thread for one user, in two queries.

    "Unread" excludes the user's own messages and tombstoned rows — a thread
    should not sit bolded because of something you wrote or something staff
    removed.
    """
    from sqlalchemy import and_, func, or_

    from db.models import ChatMessage, ChatRead

    ids = [int(t) for t in thread_ids]
    if not ids:
        return {}
    pointers = dict(
        s.query(ChatRead.thread_id, ChatRead.last_read_message_id)
        .filter(ChatRead.thread_id.in_(ids), ChatRead.user_id == user_id)
        .all()
    )
    # One grouped COUNT with a per-thread id floor, rather than a query per
    # thread or pulling every id back to count in Python.
    newer_than_pointer = or_(
        *[
            and_(
                ChatMessage.thread_id == tid,
                ChatMessage.id > int(pointers.get(tid, 0) or 0),
            )
            for tid in ids
        ]
    )
    rows = (
        s.query(ChatMessage.thread_id, func.count(ChatMessage.id))
        .filter(
            newer_than_pointer,
            ChatMessage.deleted_at.is_(None),
            # An authorless system entry still counts; only the caller's OWN
            # messages are excluded. Plain `!= user_id` would drop both,
            # because NULL != x is NULL, not true.
            or_(
                ChatMessage.author_user_id.is_(None),
                ChatMessage.author_user_id != user_id,
            ),
        )
        .group_by(ChatMessage.thread_id)
        .all()
    )
    counts: dict[int, int] = {tid: 0 for tid in ids}
    for thread_id, n in rows:
        counts[int(thread_id)] = int(n or 0)
    return counts


def last_read_id(s, thread_id: int, user_id: int) -> int:
    from db.models import ChatRead

    row = (
        s.query(ChatRead.last_read_message_id)
        .filter(ChatRead.thread_id == thread_id, ChatRead.user_id == user_id)
        .first()
    )
    return int(row[0]) if row and row[0] else 0


# --------------------------------------------------------------------------- #
# Realtime fan-out
# --------------------------------------------------------------------------- #
def thread_audience(s, thread) -> list[int]:
    """User ids to nudge when a thread gains a message: the members of every
    group party plus any user parties.

    Only used for the lightweight ``chat_unread`` ping (a badge hint). The
    message body itself goes to ``rt:chat:{id}``, which is membership-gated at
    subscribe time — so a wide audience here can never leak content.
    """
    from db.models import ChatParticipant, GroupAdmin, GroupEventManager

    participants = (
        s.query(ChatParticipant).filter(ChatParticipant.thread_id == thread.id).all()
    )
    group_ids = [int(p.party_id) for p in participants if p.party_type == "group"]
    user_ids = {int(p.party_id) for p in participants if p.party_type == "user"}
    if group_ids:
        user_ids |= {
            int(uid)
            for (uid,) in s.query(GroupAdmin.user_id)
            .filter(GroupAdmin.group_id.in_(group_ids))
            .all()
        }
        user_ids |= {
            int(uid)
            for (uid,) in s.query(GroupEventManager.user_id)
            .filter(GroupEventManager.group_id.in_(group_ids))
            .all()
        }
    return sorted(user_ids)


def publish_message(s, thread, message) -> None:
    """Push a new message to the thread's live subscribers. Best-effort."""
    try:
        from services.realtime import publish_chat_message

        audience = [
            uid for uid in thread_audience(s, thread) if uid != message.author_user_id
        ]
        publish_chat_message(
            thread_id=int(thread.id),
            payload=message_payload(message),
            audience=audience,
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _ts(dt: Optional[datetime]) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def message_payload(message, *, author_name: Optional[str] = None) -> dict:
    """Wire shape for one timeline entry. A tombstoned row keeps its id, author
    and timestamp (so the thread doesn't visibly reshuffle) but loses its
    content."""
    deleted = message.deleted_at is not None
    payload = {
        "id": int(message.id),
        "thread_id": int(message.thread_id),
        "kind": message.kind,
        "author_user_id": message.author_user_id,
        "author_name": author_name,
        "party_type": message.author_party_type,
        "party_id": message.author_party_id,
        "created_at": _ts(message.created_at),
        "deleted": deleted,
        "body": None if deleted else (message.body or None),
        "attachments": [] if deleted else _json_list(message.attachments_json),
        "system_code": message.system_code,
        "system_data": _json_obj(message.system_data_json),
    }
    return payload


def thread_payload(thread, *, participants=None, unread: int = 0,
                   membership: Optional[Membership] = None,
                   party_names: Optional[dict] = None) -> dict:
    names = party_names or {}
    return {
        "id": int(thread.id),
        "kind": thread.kind,
        "subject_type": thread.subject_type,
        "subject_id": int(thread.subject_id),
        "title": thread.title,
        "status": thread.status,
        "created_at": _ts(thread.created_at),
        "last_message_at": _ts(thread.last_message_at),
        "unread": int(unread),
        "participants": [
            {
                "party_type": p.party_type,
                "party_id": int(p.party_id),
                "role": p.role,
                "name": names.get((p.party_type, int(p.party_id))),
            }
            for p in (participants or [])
        ],
        "my_parties": [
            {
                "party_type": p.type,
                "party_id": p.id,
                "name": names.get((p.type, p.id)),
            }
            for p in (membership.parties if membership else ())
        ],
        "can_post": bool(membership.can_post) if membership else False,
        "is_moderator": bool(membership.is_moderator) if membership else False,
    }


def _json_list(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_obj(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None
