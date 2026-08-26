"""Thread delivery (web103a): reach, DM outcomes, and who may read whose names.

``services/chat_delivery`` answers "who got this?" for a relayed thread by
merging two things that disagree on purpose — the live roster a group party
resolves to, and the durable ``discord_outbox`` record of what the bot
actually sent. The invariants worth pinning:

* **Names are party-scoped.** A clan-vs-clan host may see that the challenged
  clan was notified; it must not receive that clan's leadership roster. Staff
  see everything.
* **DM targets are not the same as parties.** A CvC host is never DM'd about
  its own challenge, so it must never be counted as "missed" — that would
  report a delivery failure for a message they wrote.
* **The newest attempt wins.** A reopened notice re-fans-out; a stale
  ``failed`` must not outrank today's ``sent``.

``db`` is a conftest MagicMock, so the session here is a dispatching fake and
the outbox query is stubbed — the SQL is plumbing, the merge is the logic.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import services.chat_delivery as delivery


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _thread(kind, *, id=7, subject_id=15):
    return SimpleNamespace(id=id, kind=kind, subject_id=subject_id)


def _party(party_type, party_id, role="member", pid=1):
    return SimpleNamespace(
        id=pid, party_type=party_type, party_id=party_id, role=role
    )


def test_outbox_ref_matches_each_emitter():
    """The two fan-outs anchor to different ids and both are correct — a
    notice DM to its thread, a challenge to the event_group it is about."""
    assert delivery.outbox_ref(_thread("group_notice", id=7)) == ("group_notice", 7)
    assert delivery.outbox_ref(
        _thread("event_invite", id=7, subject_id=15)
    ) == ("event_invite", 15)


def test_outbox_ref_none_for_kinds_without_a_fan_out():
    """staff_dm relays per message, not per thread; an unknown kind has no
    contract at all. Both must report 'no fan-out expected' rather than
    silently matching some other feature's outbox rows."""
    assert delivery.outbox_ref(_thread("staff_dm")) is None
    assert delivery.outbox_ref(_thread("suggestion")) is None


def test_dm_targets_exclude_the_originating_party():
    parties = [_party("group", 267, role="owner"), _party("group", 14)]
    assert delivery._dm_targets(_thread("event_invite"), parties) == {("group", 14)}


def test_dm_targets_cover_every_group_on_a_notice():
    parties = [_party("group", 316)]
    assert delivery._dm_targets(_thread("group_notice"), parties) == {("group", 316)}


def test_dm_targets_empty_for_unknown_kinds():
    """No contract, no claim: an unrecognised kind reports nobody as a target
    so nothing it reaches gets counted as a delivery failure."""
    assert delivery._dm_targets(_thread("suggestion"), [_party("group", 1)]) == set()


def test_missed_counts_only_where_a_dm_was_aimed():
    people = [
        {"delivery": "sent"},
        {"delivery": "none"},
        {"delivery": "failed"},
    ]
    assert delivery._counts(people) == {
        "reached": 3, "sent": 1, "failed": 1, "pending": 0, "missed": 1
    }
    # Same people, party nobody tried to DM: reach stands, missed is not a
    # failure to report.
    assert delivery._counts(people, dm_target=False)["missed"] == 0


def _outbox(channel_id, status, *, id=1, at=None, error=None):
    return SimpleNamespace(
        id=id, channel_id=channel_id, status=status, error=error,
        processed_at=at, created_at=at,
    )


def test_fold_keeps_the_newest_attempt_and_counts_the_rest():
    rows = [
        _outbox("111", "failed", id=1, error="closed DMs"),
        _outbox("111", "sent", id=2),
        _outbox("222", "pending", id=3),
    ]
    folded = delivery.fold_dm_rows(rows)
    assert folded["111"]["status"] == "sent"
    assert folded["111"]["error"] is None
    assert folded["111"]["attempts"] == 2
    assert folded["222"]["attempts"] == 1


def test_fold_ignores_rows_without_a_recipient():
    assert delivery.fold_dm_rows([_outbox("", "sent"), _outbox(None, "sent")]) == {}


def test_fold_reports_the_processed_timestamp():
    stamp = datetime(2026, 8, 25, 20, 51, 34)
    folded = delivery.fold_dm_rows([_outbox("111", "sent", at=stamp)])
    assert folded["111"]["at"] == int(stamp.timestamp())


@pytest.mark.parametrize(
    "raw,expected",
    [("sent", "sent"), ("failed", "failed"), ("pending", "pending"),
     ("sending", "pending"), (None, "none")],
)
def test_sending_reads_as_pending(raw, expected):
    """`sending` is a mid-drain claim — indistinguishable from pending to
    anyone reading the panel, and reporting it verbatim would leak queue
    mechanics into a user-facing status."""
    dm = {"status": raw} if raw else None
    got = delivery._recipient(
        user_id=1, name="x", discord_id="1", role="admin", dm=dm
    )
    assert got["delivery"] == expected


# --------------------------------------------------------------------------- #
# thread_delivery: the merge, and who sees names
# --------------------------------------------------------------------------- #
class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _Session:
    """Dispatches on the queried entity/column.

    ``db.models`` is a MagicMock, so its attributes are stable identities we
    can match against. ``users`` takes a list because the module queries that
    table twice with different filters (known ids, then unresolved Discord
    ids) and a fake filter cannot tell them apart.
    """

    def __init__(self, **plan):
        self.plan = {k: (v if isinstance(v, list) and v and isinstance(v[0], list)
                         else [v])
                     for k, v in plan.items()}

    def query(self, *cols):
        from db.models import (
            ChatParticipant, Group, GroupAdmin, GroupEventManager, User,
        )

        head = cols[0]
        key = None
        for name, obj in (
            ("participants", ChatParticipant),
            ("admins", GroupAdmin.group_id),
            ("managers", GroupEventManager.group_id),
            ("users", User.user_id),
            ("groups", Group.group_id),
        ):
            if head is obj:
                key = name
                break
        seq = self.plan.get(key) or [[]]
        return _Rows(seq.pop(0) if len(seq) > 1 else seq[0])


def _membership(parties=()):
    return SimpleNamespace(
        parties=tuple(SimpleNamespace(type=t, id=i) for t, i in parties),
        # Superadmin-only, and deliberately NOT what unredacts the payload —
        # see the `staff=` kwarg.
        is_moderator=False,
        can_post=True,
    )


def _cvc_session():
    """Host clan 267 (2 admins), challenged clan 14 (1 admin, 1 manager)."""
    return _Session(
        participants=[
            _party("group", 267, role="owner", pid=1),
            _party("group", 14, pid=2),
        ],
        admins=[(267, 2, "owner"), (267, 5, "admin"), (14, 3, "owner")],
        managers=[(14, 86)],
        users=[
            (2, "koeppy", "1002"),
            (5, "joelhalen", "1005"),
            (3, "brondt_", "1003"),
            (86, "izuny.", "1086"),
        ],
        groups=[(267, "DropTracker Test"), (14, "Pegasus PvM")],
    )


def _patch_dms(monkeypatch, dms):
    monkeypatch.setattr(delivery, "_dm_rows", lambda s, thread: dms)


def test_host_sees_its_own_names_and_only_counts_for_the_other_clan(monkeypatch):
    _patch_dms(monkeypatch, {"1003": {"status": "sent", "at": 1, "error": None,
                                      "attempts": 1}})
    got = delivery.thread_delivery(
        _cvc_session(), _thread("event_invite"),
        membership=_membership(parties=[("group", 267)]),
    )
    host, guest = got["parties"]

    assert host["name"] == "DropTracker Test"
    assert [r["name"] for r in host["recipients"]] == ["koeppy", "joelhalen"]

    assert guest["name"] == "Pegasus PvM"
    assert guest["visible"] is False
    assert guest["recipients"] == []
    # The useful half survives the redaction: they know it was delivered.
    assert guest["counts"]["sent"] == 1
    assert guest["counts"]["reached"] == 2


def test_developer_staff_see_every_party_without_being_superadmin(monkeypatch):
    """`is_moderator` is superadmin-only, so gating names on it would have hidden
    the panel from the developers who spend the most time in these threads."""
    _patch_dms(monkeypatch, {})
    got = delivery.thread_delivery(
        _cvc_session(), _thread("event_invite"),
        membership=_membership(), staff=True,
    )
    assert all(p["visible"] for p in got["parties"])
    assert [r["name"] for r in got["parties"][1]["recipients"]] == [
        "brondt_", "izuny."
    ]


def test_host_is_never_reported_as_a_missed_delivery(monkeypatch):
    """The host wrote the challenge; nobody DM's them about it. Counting the
    host's admins as 'missed' would show a delivery failure that never was."""
    _patch_dms(monkeypatch, {"1003": {"status": "sent", "at": 1, "error": None,
                                      "attempts": 1}})
    got = delivery.thread_delivery(
        _cvc_session(), _thread("event_invite"),
        membership=_membership(), staff=True,
    )
    host, guest = got["parties"]
    assert host["dm_target"] is False and host["counts"]["missed"] == 0
    # The event manager on the challenged clan genuinely got nothing.
    assert guest["dm_target"] is True and guest["counts"]["missed"] == 1
    assert got["counts"] == {"reached": 4, "sent": 1, "failed": 0,
                            "pending": 0, "missed": 1}


def test_reached_but_undelivered_people_are_still_listed(monkeypatch):
    """The MANAGE_GUILD-only admin gap: somebody can hold a seat on the thread
    and never be DM-able. Dropping them from the list would hide exactly the
    person an administrator is looking for."""
    _patch_dms(monkeypatch, {})
    got = delivery.thread_delivery(
        _cvc_session(), _thread("event_invite"),
        membership=_membership(), staff=True,
    )
    guest = got["parties"][1]
    assert {r["name"]: r["delivery"] for r in guest["recipients"]} == {
        "brondt_": "none", "izuny.": "none"
    }


def _notice_session(users_known, users_stray):
    return _Session(
        participants=[_party("group", 316, pid=1)],
        admins=[(316, 3337, "owner")],
        managers=[],
        users=[users_known, users_stray],
        groups=[(316, "Veylor")],
    )


def test_a_lone_group_party_owns_its_unrecognised_recipients(monkeypatch):
    """Notice fan-out also DMs the legacy bot-side `authed_users` list, so a
    recipient who holds no web grant is normal, not an anomaly. With one clan
    on the thread there is nowhere else they could have come from."""
    _patch_dms(monkeypatch, {
        "1337": {"status": "sent", "at": 1, "error": None, "attempts": 1},
        "9999": {"status": "failed", "at": 2, "error": "closed", "attempts": 1},
    })
    s = _notice_session([(3337, "ricky0399", "1337")], [(4000, "olddawg", "9999")])
    got = delivery.thread_delivery(
        s, _thread("group_notice"),
        membership=_membership(parties=[("group", 316)]),
    )
    party = got["parties"][0]
    assert {r["name"] for r in party["recipients"]} == {"ricky0399", "olddawg"}
    assert party["counts"]["sent"] == 1 and party["counts"]["failed"] == 1
    assert got["others"] == [] and got["others_count"] == 0


def test_unattributable_recipients_are_counted_but_named_only_for_staff(monkeypatch):
    """Two clans on the thread: a DM'd person who resolves to neither roster
    cannot be attributed, and is somebody's ex-leadership either way."""
    dms = {"7777": {"status": "sent", "at": 1, "error": None, "attempts": 1}}
    _patch_dms(monkeypatch, dms)

    def _session():
        s = _cvc_session()
        s.plan["users"] = [
            [(2, "koeppy", "1002"), (5, "joelhalen", "1005"),
             (3, "brondt_", "1003"), (86, "izuny.", "1086")],
            [(900, "demoted", "7777")],
        ]
        return s

    member = delivery.thread_delivery(
        _session(), _thread("event_invite"),
        membership=_membership(parties=[("group", 267)]),
    )
    assert member["others"] == []
    assert member["others_count"] == 1
    assert member["counts"]["sent"] == 1

    staff = delivery.thread_delivery(
        _session(), _thread("event_invite"), membership=_membership(), staff=True
    )
    assert [r["name"] for r in staff["others"]] == ["demoted"]


def test_no_membership_yields_counts_only(monkeypatch):
    _patch_dms(monkeypatch, {})
    got = delivery.thread_delivery(
        _cvc_session(), _thread("event_invite"), membership=None
    )
    assert [p["visible"] for p in got["parties"]] == [False, False]
    assert all(p["recipients"] == [] for p in got["parties"])
    assert got["counts"]["reached"] == 4


def test_party_cap_reports_what_it_dropped(monkeypatch):
    """A capped list that does not say it was capped reads as a complete one."""
    _patch_dms(monkeypatch, {})
    got = delivery.thread_delivery(
        _cvc_session(), _thread("event_invite"),
        membership=_membership(), staff=True, party_cap=1,
    )
    host = got["parties"][0]
    assert len(host["recipients"]) == 1
    assert host["hidden"] == 1
    assert host["counts"]["reached"] == 2


def test_explicit_admin_grant_outranks_the_event_manager_one(monkeypatch):
    _patch_dms(monkeypatch, {})
    s = _Session(
        participants=[_party("group", 14, pid=1)],
        admins=[(14, 3, "owner")],
        managers=[(14, 3)],
        users=[(3, "brondt_", "1003")],
        groups=[(14, "Pegasus PvM")],
    )
    got = delivery.thread_delivery(
        s, _thread("group_notice"), membership=_membership(), staff=True
    )
    assert [r["role"] for r in got["parties"][0]["recipients"]] == ["owner"]


def test_dm_expected_is_false_where_no_fan_out_exists(monkeypatch):
    """So the UI can say 'no DMs are sent for this' instead of implying that
    every recipient was missed."""
    _patch_dms(monkeypatch, {})
    s = _Session(
        participants=[_party("user", 42, pid=1)],
        users=[(42, "someone", "1042")],
    )
    got = delivery.thread_delivery(
        s, _thread("staff_dm"), membership=_membership(), staff=True
    )
    assert got["dm_expected"] is False
