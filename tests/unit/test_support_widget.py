"""Support widget backend (web102a): the invariants that keep the site↔Discord
bridge from echoing, spamming, or lying about unread state.

* **The relay marker is the echo firewall.** A web ticket reply reaches
  Discord as a bot-authored ``**name** (via site): …`` message; the transcript
  mirror must skip exactly those and nothing else — a human typing the same
  text must still mirror, and the bot's other content must keep mirroring.
* **The own-reply floor.** Ticket/suggestion unread is counted above
  ``max(pointer, own latest message id)``, which is what makes "they already
  answered in Discord" show zero on the site with no pointer row at all.
* **Staff membership is kind-scoped.** ``resolve_membership``'s staff branch
  seats developers on staff_dm/group_notice threads only — a CvC negotiation
  must stay a 404 to staff who hold no party on it.
* **DM relay content always fits.** Discord hard-caps at 2000; the prefix and
  body must be jointly truncated, never rejected.

``db`` is a conftest MagicMock — these drive real functions with fakes.
"""
from types import SimpleNamespace

import pytest

import services.chat as chat
from services.inbox import _floors
from services.staff_dm import relay_dm_content, thread_url
from services.ticket_transcripts import _is_web_relay


# --------------------------------------------------------------------------- #
# _is_web_relay: the echo firewall
# --------------------------------------------------------------------------- #
def _author(bot: bool):
    return SimpleNamespace(bot=bot)


@pytest.mark.parametrize(
    "content",
    [
        "**alice** (via site): hi there",
        "**user with spaces** (via site): body",
        "**a** (via site): multi\nline\nbody",
    ],
)
def test_relay_marker_skips_bot_copies(content):
    assert _is_web_relay(_author(True), content) is True


def test_relay_marker_never_skips_humans():
    """A human pasting the exact marker text must still mirror — the bot
    check, not the regex, carries the guarantee."""
    assert _is_web_relay(_author(False), "**alice** (via site): hi") is False


@pytest.mark.parametrize(
    "content",
    [
        "plain bot announcement",
        "**bold lead** without the marker",
        "(via site): missing the name part",
        "",
        None,
    ],
)
def test_relay_marker_leaves_other_bot_content(content):
    assert _is_web_relay(_author(True), content) is False


def test_relay_marker_matches_route_builder():
    """The route's relay builder and the mirror's matcher must agree — this is
    the pair that, drifting apart, becomes a duplicate-message bug."""
    from web_api.routes.tickets import _relay_content

    assert _is_web_relay(_author(True), _relay_content("alice", "hello")) is True
    # 100-char names are the builder's cap; the matcher must still bite.
    assert _is_web_relay(_author(True), _relay_content("x" * 300, "hello")) is True


# --------------------------------------------------------------------------- #
# The own-reply floor
# --------------------------------------------------------------------------- #
def test_floor_is_max_of_pointer_and_own_reply():
    floors = _floors({1: 5}, {1: 9}, [1])
    assert floors[1] == 9
    floors = _floors({1: 12}, {1: 9}, [1])
    assert floors[1] == 12


def test_floor_defaults_to_zero_without_state():
    """Cold start: no pointer row, never replied — everything is unread."""
    assert _floors({}, {}, [3]) == {3: 0}


def test_floor_covers_discord_reply_without_pointer():
    """The load-bearing case: a user who replied in Discord (mirrored as their
    own latest row) has floor == that row even though no pointer exists."""
    assert _floors({}, {7: 41}, [7]) == {7: 41}


def test_floor_none_values_are_zero():
    assert _floors({1: None}, {1: None}, [1]) == {1: 0}


# --------------------------------------------------------------------------- #
# resolve_membership: staff branch is kind-scoped
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
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


def _participant(party_type, party_id, pid=1):
    return SimpleNamespace(
        id=pid, party_type=party_type, party_id=party_id, role="member"
    )


def _patch_deps(monkeypatch, *, support_staff, superadmin=False):
    import sys
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.resolve_group_role = lambda s, uid, gid, mg=None, user=None: None
    fake.is_event_manager = lambda s, uid, gid: False
    fake.is_superadmin = lambda user: superadmin
    fake.is_support_staff = lambda user: support_staff
    fake.load_user = lambda s, uid: SimpleNamespace(user_id=uid, groups=[])
    fake.manageable_guild_ids = lambda uid: set()
    monkeypatch.setitem(sys.modules, "web_api.deps", fake)


def _thread(kind, status="open"):
    return SimpleNamespace(id=7, status=status, kind=kind)


@pytest.mark.parametrize("kind", ["staff_dm", "group_notice"])
def test_staff_seated_on_support_kinds(monkeypatch, kind):
    _patch_deps(monkeypatch, support_staff=True)
    s = _FakeSession([_participant("user", 42)])
    got = chat.resolve_membership(s, _thread(kind), user_id=99)
    assert got is not None
    assert got.parties == (chat.Party("user", 99),)
    assert got.can_post is True


def test_staff_branch_respects_thread_status(monkeypatch):
    _patch_deps(monkeypatch, support_staff=True)
    s = _FakeSession([_participant("user", 42)])
    got = chat.resolve_membership(s, _thread("staff_dm", status="locked"), user_id=99)
    assert got is not None and got.can_post is False


def test_staff_not_seated_on_event_invites(monkeypatch):
    """A CvC negotiation stays invisible: the staff branch must never widen
    the original kinds."""
    _patch_deps(monkeypatch, support_staff=True)
    s = _FakeSession([_participant("group", 42)])
    assert chat.resolve_membership(s, _thread("event_invite"), user_id=99) is None


def test_non_staff_still_fails_closed(monkeypatch):
    _patch_deps(monkeypatch, support_staff=False)
    s = _FakeSession([_participant("user", 42)])
    assert chat.resolve_membership(s, _thread("staff_dm"), user_id=99) is None


def test_subject_user_matches_by_party_not_staff_branch(monkeypatch):
    """The target of a staff chat holds a real user party; the staff branch is
    never consulted for them."""
    _patch_deps(monkeypatch, support_staff=False)
    s = _FakeSession([_participant("user", 42)])
    got = chat.resolve_membership(s, _thread("staff_dm"), user_id=42)
    assert got is not None and got.parties == (chat.Party("user", 42),)


# --------------------------------------------------------------------------- #
# Participant fan-out: user_id 0 is a real account
# --------------------------------------------------------------------------- #
class _EmptyQuery:
    def filter(self, *a, **k):
        return self

    def distinct(self):
        return self

    def all(self):
        return []


class _EmptySession:
    def query(self, *a, **k):
        return _EmptyQuery()


def test_ticket_participants_include_user_zero():
    """`if ticket.created_by:` is false for the account whose id is 0 — which
    is a real, active account here — and would drop that person from their own
    ticket's badge fan-out. Presence must be tested, not truthiness."""
    from services.inbox import ticket_participant_user_ids

    ticket = SimpleNamespace(ticket_id=1, created_by=0, claimed_by=None)
    assert ticket_participant_user_ids(_EmptySession(), ticket) == {0}


def test_ticket_participants_include_negative_ids():
    from services.inbox import ticket_participant_user_ids

    ticket = SimpleNamespace(ticket_id=1, created_by=-1, claimed_by=0)
    assert ticket_participant_user_ids(_EmptySession(), ticket) == {-1, 0}


def test_ticket_participants_skip_missing_claimer():
    from services.inbox import ticket_participant_user_ids

    ticket = SimpleNamespace(ticket_id=1, created_by=5, claimed_by=None)
    assert ticket_participant_user_ids(_EmptySession(), ticket) == {5}


def test_suggestion_participants_include_user_zero():
    from services.inbox import suggestion_participant_user_ids

    sug = SimpleNamespace(id=1, user_id=0)
    assert suggestion_participant_user_ids(_EmptySession(), sug) == {0}


def test_publish_inbox_unread_excludes_author_zero(monkeypatch):
    """The author-exclusion compares with `is not None` too — excluding user 0
    must actually exclude them, and must not exclude everyone else."""
    import sys
    from types import ModuleType

    from services import inbox as inbox_mod

    sent = []
    fake = ModuleType("services.realtime")
    fake.publish_event = lambda t, scope, data: sent.append((t, scope, data))
    monkeypatch.setitem(sys.modules, "services.realtime", fake)

    inbox_mod.publish_inbox_unread("ticket", 9, [0, 5], exclude_user_id=0)
    assert [s[1] for s in sent] == ["user:5"]

    sent.clear()
    inbox_mod.publish_inbox_unread("ticket", 9, [0, 5])
    assert sorted(s[1] for s in sent) == ["user:0", "user:5"]


# --------------------------------------------------------------------------- #
# Relay DM content
# --------------------------------------------------------------------------- #
def test_relay_dm_fits_discord_cap():
    out = relay_dm_content("x" * 300, "y" * 5000)
    assert len(out) <= 2000
    assert out.startswith("**" + "x" * 100)
    assert "(DropTracker staff):" in out


def test_relay_dm_empty_body_names_attachment():
    assert relay_dm_content("staff", "").endswith("(attachment)")
    assert relay_dm_content("staff", None).endswith("(attachment)")


def test_thread_url_shape():
    assert thread_url(12) == "https://www.droptracker.io/messages/12"


# --------------------------------------------------------------------------- #
# New kinds/codes are registered everywhere they must be
# --------------------------------------------------------------------------- #
def test_thread_kinds_and_codes_registered():
    assert "staff_dm" in chat.THREAD_KINDS
    assert "group_notice" in chat.THREAD_KINDS
    for code in (
        "staff_dm_opened",
        "dm_bounced",
        "notice_raised",
        "notice_recurred",
        "notice_resolved",
    ):
        assert code in chat.SYSTEM_CODES


def test_group_notice_auto_resolve_codes_are_prefixed():
    """The success-path resolver must only ever touch bot-raised codes; a
    superadmin's manual notice code must not appear here."""
    from services.group_notices import AUTO_RESOLVE_ON_GROUP_SEND

    assert set(AUTO_RESOLVE_ON_GROUP_SEND) == {
        "notify_channel_forbidden",
        "notify_channel_missing",
        "event_alert_forbidden",
        "event_alert_no_channel",
    }
