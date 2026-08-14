"""Chat subsystem (web96a): the decisions that decide who can read a clan's
private negotiation and what may end up rendered inside it.

The load-bearing properties, each of which is a real leak or a real bug if it
slips:

* **A group party resolves to humans live.** Nothing stores a roster, so an
  event manager can answer a challenge and a demoted admin stops being able to
  — with no backfill. A plain clan member must never resolve at all.
* **Attachment keys are ours or they don't render.** The client hands back a
  key from our own upload endpoint; a client-supplied URL, or a key outside the
  upload prefix, would put an arbitrary remote image inside our chrome.
* **Read pointers only ever advance.** A stale tab reporting an old id must not
  resurrect a badge the user already cleared.
* **Unread excludes your own messages but not authorless system entries.**
  ``author_user_id != user_id`` alone drops the latter, because NULL != x is
  NULL — the bug this counts on not having.

``db`` is a conftest MagicMock, so these tests drive the real service functions
with hand-built fakes rather than an ORM session.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

import services.chat as chat


# --------------------------------------------------------------------------- #
# Bodies and attachments
# --------------------------------------------------------------------------- #
def test_normalize_body_trims():
    assert chat.normalize_body("  hello  ") == "hello"
    assert chat.normalize_body(None) == ""


def test_normalize_body_enforces_cap():
    ok = "x" * chat.BODY_MAX_CHARS
    assert chat.normalize_body(ok) == ok
    with pytest.raises(chat.ChatError):
        chat.normalize_body("x" * (chat.BODY_MAX_CHARS + 1))


def test_attachments_empty_forms():
    assert chat.normalize_attachments(None) == []
    assert chat.normalize_attachments("") == []
    assert chat.normalize_attachments([]) == []


def test_attachments_reject_foreign_keys():
    """The whole point of keeping the key and re-deriving the URL: a caller
    must not be able to name an object we never issued."""
    for bad in (
        [{"key": "https://evil.example/pwn.png"}],
        [{"key": "dt_transfers/someone-elses-private-file"}],
        [{"key": "dt_uploads/../../etc/passwd"}],
        [{"key": ""}],
        [{"url": "https://cdn.example/x.png"}],  # no key at all
        ["dt_uploads/abc.png"],                  # not an object
    ):
        with pytest.raises(chat.ChatError):
            chat.normalize_attachments(bad)


def test_attachments_ignore_client_supplied_url(monkeypatch):
    monkeypatch.setattr(chat, "attachment_url", lambda key: f"https://cdn.test/{key}")
    out = chat.normalize_attachments(
        [{"key": "dt_uploads/abc.png", "url": "https://evil.example/pwn.png"}]
    )
    assert out == [
        {"key": "dt_uploads/abc.png", "url": "https://cdn.test/dt_uploads/abc.png"}
    ]


def test_attachments_cap(monkeypatch):
    monkeypatch.setattr(chat, "attachment_url", lambda key: key)
    entries = [{"key": f"dt_uploads/{i}.png"} for i in range(chat.MAX_ATTACHMENTS + 1)]
    with pytest.raises(chat.ChatError):
        chat.normalize_attachments(entries)


# --------------------------------------------------------------------------- #
# Membership resolution — the authorization funnel
# --------------------------------------------------------------------------- #
class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Returns a canned participant list for any query."""

    def __init__(self, rows):
        self._rows = rows
        self.added = []

    def query(self, *a, **k):
        return _FakeQuery(self._rows)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def flush(self):
        pass


def _participant(party_type, party_id, pid=1):
    return SimpleNamespace(
        id=pid, party_type=party_type, party_id=party_id, role="member"
    )


def _patch_deps(monkeypatch, *, role=None, event_manager=False, superadmin=False):
    """Stand in for web_api.deps, which services.chat lazy-imports."""
    import sys
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.resolve_group_role = lambda s, uid, gid, mg=None, user=None: role
    fake.is_event_manager = lambda s, uid, gid: event_manager
    fake.is_superadmin = lambda user: superadmin
    fake.load_user = lambda s, uid: SimpleNamespace(user_id=uid, groups=[])
    fake.manageable_guild_ids = lambda uid: set()
    fake.event_manager_group_ids = lambda s, uid: set()
    monkeypatch.setitem(sys.modules, "web_api.deps", fake)


THREAD = SimpleNamespace(id=7, status="open")


@pytest.mark.parametrize(
    "role,manager,expected",
    [
        ("owner", False, True),
        ("admin", False, True),
        # web64a: an event manager holds no group-admin right but fully manages
        # the group's events — excluding them from the challenge they are
        # running would be incoherent.
        (None, True, True),
        ("member", True, True),
        # A plain member of the clan is NOT a party to its negotiations.
        ("member", False, False),
        (None, False, False),
    ],
)
def test_group_party_membership(monkeypatch, role, manager, expected):
    _patch_deps(monkeypatch, role=role, event_manager=manager)
    s = _FakeSession([_participant("group", 42)])
    got = chat.resolve_membership(s, THREAD, user_id=99)
    assert (got is not None) is expected
    if expected:
        assert got.parties == (chat.Party("group", 42),)
        assert got.can_post is True


def test_user_party_matches_by_identity(monkeypatch):
    _patch_deps(monkeypatch, role=None)
    s = _FakeSession([_participant("user", 5)])
    assert chat.resolve_membership(s, THREAD, user_id=5) is not None
    assert chat.resolve_membership(s, THREAD, user_id=6) is None


def test_membership_is_none_for_user_zero_safe(monkeypatch):
    """user_id 0 is a real account on this platform (the site owner), so the
    guards must test `is None`, never truthiness."""
    _patch_deps(monkeypatch, role=None)
    s = _FakeSession([_participant("user", 0)])
    assert chat.resolve_membership(s, THREAD, user_id=0) is not None


def test_dual_admin_gets_both_parties(monkeypatch):
    """Somebody who administers BOTH clans in a battle is handed both hats
    rather than having one silently picked for them."""
    _patch_deps(monkeypatch, role="admin")
    s = _FakeSession([_participant("group", 1, pid=1), _participant("group", 2, pid=2)])
    got = chat.resolve_membership(s, THREAD, user_id=99)
    assert got.parties == (chat.Party("group", 1), chat.Party("group", 2))
    assert got.allows(chat.Party("group", 2)) is True
    assert got.allows(chat.Party("group", 3)) is False


def test_locked_thread_is_readable_but_not_postable(monkeypatch):
    _patch_deps(monkeypatch, role="admin")
    s = _FakeSession([_participant("group", 42)])
    got = chat.resolve_membership(s, SimpleNamespace(id=7, status="locked"), user_id=99)
    assert got is not None
    assert got.can_post is False


def test_membership_fails_closed_on_missing_inputs(monkeypatch):
    _patch_deps(monkeypatch, role="owner")
    assert chat.resolve_membership(_FakeSession([]), None, user_id=1) is None
    assert chat.resolve_membership(_FakeSession([]), THREAD, user_id=None) is None
    # No participants at all -> nobody is a member.
    assert chat.resolve_membership(_FakeSession([]), THREAD, user_id=1) is None


# --------------------------------------------------------------------------- #
# Membership value object
# --------------------------------------------------------------------------- #
def test_membership_allows_defaults_to_primary():
    m = chat.Membership(parties=(chat.Party("group", 1),), can_post=True)
    assert m.primary == chat.Party("group", 1)
    assert m.allows(None) is True
    assert chat.Membership(parties=(), can_post=True).allows(None) is False


# --------------------------------------------------------------------------- #
# Read pointer
# --------------------------------------------------------------------------- #
class _Read:
    def __init__(self, last):
        self.thread_id = 7
        self.user_id = 1
        self.last_read_message_id = last
        self.updated_at = None


def _read_session(existing):
    class S(_FakeSession):
        def query(self, *a, **k):
            return _FakeQuery([existing] if existing else [])

    return S([])


def test_mark_read_advances(monkeypatch):
    row = _Read(10)
    s = _read_session(row)
    chat.mark_read(s, 7, 1, 25)
    assert row.last_read_message_id == 25


def test_mark_read_never_rewinds(monkeypatch):
    """A background tab replaying an old id must not un-read the thread."""
    row = _Read(25)
    s = _read_session(row)
    chat.mark_read(s, 7, 1, 10)
    assert row.last_read_message_id == 25


def test_mark_read_creates_missing_pointer(monkeypatch):
    import sys
    from unittest.mock import MagicMock

    created = {}

    class _ChatRead:
        # Class attributes so the service's `ChatRead.thread_id == ...` filter
        # expression resolves; the fake session ignores the result anyway.
        thread_id = None
        user_id = None

        def __init__(self, **kw):
            created.update(kw)
            self.last_read_message_id = kw.get("last_read_message_id")

    fake_models = MagicMock()
    fake_models.ChatRead = _ChatRead
    monkeypatch.setitem(sys.modules, "db.models", fake_models)

    s = _read_session(None)
    chat.mark_read(s, 7, 1, 33)
    assert created["last_read_message_id"] == 33


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _message(**kw):
    base = dict(
        id=3,
        thread_id=7,
        kind="message",
        author_user_id=11,
        author_party_type="group",
        author_party_id=42,
        body="hi",
        attachments_json=None,
        system_code=None,
        system_data_json=None,
        created_at=datetime(2026, 8, 14, 12, 0, 0),
        deleted_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_message_payload_basic():
    out = chat.message_payload(_message(), author_name="Zezima")
    assert out["body"] == "hi"
    assert out["author_name"] == "Zezima"
    assert out["deleted"] is False
    assert out["attachments"] == []


def test_tombstoned_message_keeps_its_place_but_loses_content():
    """A takedown must not reshuffle the timeline — id, author and timestamp
    survive so the surrounding conversation still reads correctly."""
    out = chat.message_payload(
        _message(
            deleted_at=datetime(2026, 8, 14, 13, 0, 0),
            attachments_json='[{"key": "dt_uploads/x.png", "url": "u"}]',
        )
    )
    assert out["deleted"] is True
    assert out["body"] is None
    assert out["attachments"] == []
    assert out["id"] == 3
    assert out["created_at"] is not None


def test_message_payload_tolerates_corrupt_json():
    out = chat.message_payload(
        _message(attachments_json="{not json", system_data_json="also not json")
    )
    assert out["attachments"] == []
    assert out["system_data"] is None


def test_system_payload_carries_code_and_nouns():
    out = chat.message_payload(
        _message(
            kind="system",
            body=None,
            system_code="invite_accepted",
            system_data_json='{"event_name": "Clash"}',
        )
    )
    assert out["system_code"] == "invite_accepted"
    assert out["system_data"] == {"event_name": "Clash"}


def test_thread_payload_exposes_capabilities():
    thread = SimpleNamespace(
        id=7,
        kind="event_invite",
        subject_type="event_group",
        subject_id=3,
        title="A vs B",
        status="open",
        created_at=datetime(2026, 8, 14, 10, 0, 0),
        last_message_at=datetime(2026, 8, 14, 11, 0, 0),
    )
    membership = chat.Membership(parties=(chat.Party("group", 42),), can_post=True)
    out = chat.thread_payload(
        thread,
        participants=[_participant("group", 42)],
        unread=4,
        membership=membership,
        party_names={("group", 42): "Clan B"},
    )
    assert out["unread"] == 4
    assert out["can_post"] is True
    assert out["my_parties"] == [
        {"party_type": "group", "party_id": 42, "name": "Clan B"}
    ]
    assert out["participants"][0]["name"] == "Clan B"


def test_thread_payload_without_membership_cannot_post():
    thread = SimpleNamespace(
        id=7, kind="event_invite", subject_type="event_group", subject_id=3,
        title=None, status="open", created_at=None, last_message_at=None,
    )
    out = chat.thread_payload(thread)
    assert out["can_post"] is False
    assert out["my_parties"] == []


# --------------------------------------------------------------------------- #
# System codes
# --------------------------------------------------------------------------- #
def test_post_system_rejects_unknown_code():
    with pytest.raises(chat.ChatError):
        chat.post_system(
            _FakeSession([]),
            thread=SimpleNamespace(id=7, status="open", last_message_at=None),
            code="not_a_real_code",
        )


def test_invite_lifecycle_codes_are_defined():
    for code in ("invite_sent", "invite_accepted", "invite_declined",
                 "invite_withdrawn"):
        assert code in chat.SYSTEM_CODES
