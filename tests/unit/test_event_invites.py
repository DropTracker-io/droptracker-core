"""Clan-vs-clan challenge notifications (web96a).

Before this, inviting a clan wrote a row and told nobody. The properties that
make it actually reach a human — and that fail quietly if they regress:

* **Recipients are the people who can answer.** ``group_admins`` ∪
  ``group_event_managers``, which is exactly the set the accept/decline gate
  admits. A DM to somebody who then gets a 403 is worse than no DM.
* **The opt-out defaults to ON.** This is a duty notification ("your clan has
  been challenged and someone is waiting"), not a supporter perk, so only an
  explicit "false" row suppresses it — no backfill, no silent non-delivery.
* **The button URL is the clan's own invitation page.** Wrong ids here send a
  leader to a page they cannot act on.
* **Announcing is best-effort.** The invitation row is already committed when
  these run; a Discord or Redis hiccup must not turn a successful invite into a
  500 that the host retries into a duplicate.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import services.event_invites as invites


# --------------------------------------------------------------------------- #
# URL + embed
# --------------------------------------------------------------------------- #
def test_invitation_url_points_at_the_invited_clans_page():
    url = invites.invitation_url(42, 7)
    assert url.endswith("/groups/42/events/invitations/7")
    assert url.startswith("https://")


def _event(**kw):
    base = dict(
        id=7,
        group_id=1,
        name="Autumn Clash",
        description=None,
        starts_at=datetime(2026, 9, 1, 18, 0, 0),
        ends_at=datetime(2026, 9, 8, 18, 0, 0),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_embed_names_both_clans_and_carries_timestamps():
    embed = invites.build_invite_embed(
        event=_event(), host_group_name="Clan A", invited_group_name="Clan B"
    )
    assert "Clan A" in embed["description"]
    assert "Clan B" in embed["description"]
    # Embed titles never render markdown, so the link rides on embed.url.
    assert "Autumn Clash" in embed["title"]
    assert "[" not in embed["title"]
    assert embed["url"].endswith("/events/7")
    names = {f["name"] for f in embed["fields"]}
    assert {"Starts", "Ends"} <= names


def test_embed_survives_missing_names_and_dates():
    embed = invites.build_invite_embed(
        event=_event(starts_at=None, ends_at=None, name=None),
        host_group_name=None,
        invited_group_name=None,
    )
    assert embed["fields"] == []
    assert "Another clan" in embed["description"]


def test_embed_truncates_a_long_description():
    embed = invites.build_invite_embed(
        event=_event(description="x" * 5000),
        host_group_name="A",
        invited_group_name="B",
    )
    detail = [f for f in embed["fields"] if f["name"] == "Details"][0]
    assert len(detail["value"]) <= 1000


# --------------------------------------------------------------------------- #
# Recipient resolution
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _RecipientSession:
    """Routes each query by the first entity the caller asked for."""

    def __init__(self, *, admins=(), managers=(), configs=(), users=()):
        self._by_name = {
            "GroupAdmin": [(uid,) for uid in admins],
            "GroupEventManager": [(uid,) for uid in managers],
            "UserConfiguration": list(configs),
            "User": list(users),
        }
        self.added = []

    def query(self, *entities):
        first = entities[0]
        owner = getattr(first, "_owner_name", None) or getattr(
            first, "__name__", ""
        )
        return _Q(self._by_name.get(owner, []))

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass


class _Col:
    """Stands in for a column: carries which model it belongs to."""

    def __init__(self, owner):
        self._owner_name = owner

    def in_(self, *a):
        return self

    def __eq__(self, other):
        return self


def _install_models(monkeypatch):
    import sys
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.GroupAdmin = SimpleNamespace(
        user_id=_Col("GroupAdmin"), group_id=_Col("GroupAdmin")
    )
    fake.GroupEventManager = SimpleNamespace(
        user_id=_Col("GroupEventManager"), group_id=_Col("GroupEventManager")
    )
    fake.UserConfiguration = SimpleNamespace(
        user_id=_Col("UserConfiguration"),
        config_key=_Col("UserConfiguration"),
        config_value=_Col("UserConfiguration"),
    )
    fake.User = SimpleNamespace(user_id=_Col("User"), discord_id=_Col("User"))
    monkeypatch.setitem(sys.modules, "db.models", fake)


def test_recipients_union_admins_and_event_managers(monkeypatch):
    _install_models(monkeypatch)
    s = _RecipientSession(
        admins=[1, 2],
        managers=[2, 3],  # 2 is both — must not be DM'd twice
        users=[(1, "111"), (2, "222"), (3, "333")],
    )
    got = invites.dm_recipients(s, group_id=42)
    assert got == [(1, "111"), (2, "222"), (3, "333")]


def test_recipients_default_to_opted_in(monkeypatch):
    """No user_configurations row means yes — shipping this must not require
    a backfill or leave every clan silently unnotified."""
    _install_models(monkeypatch)
    s = _RecipientSession(admins=[1], users=[(1, "111")], configs=[])
    assert invites.dm_recipients(s, 42) == [(1, "111")]


def test_explicit_opt_out_is_respected(monkeypatch):
    _install_models(monkeypatch)
    s = _RecipientSession(
        admins=[1, 2],
        users=[(1, "111"), (2, "222")],
        configs=[(1, "false")],
    )
    assert invites.dm_recipients(s, 42) == [(2, "222")]


def test_users_without_a_discord_id_are_skipped(monkeypatch):
    _install_models(monkeypatch)
    s = _RecipientSession(admins=[1, 2], users=[(1, None), (2, "222")])
    assert invites.dm_recipients(s, 42) == [(2, "222")]


def test_no_admins_means_no_queries_and_no_dms(monkeypatch):
    _install_models(monkeypatch)
    assert invites.dm_recipients(_RecipientSession(), 42) == []


def test_recipient_fan_out_is_bounded(monkeypatch):
    """A bulk invite of 30 clans must not queue an unbounded pile of rows."""
    _install_models(monkeypatch)
    many = list(range(invites.MAX_DM_RECIPIENTS + 10))
    s = _RecipientSession(
        admins=many, users=[(uid, str(uid)) for uid in many]
    )
    assert len(invites.dm_recipients(s, 42)) == invites.MAX_DM_RECIPIENTS


# --------------------------------------------------------------------------- #
# Best-effort contract
# --------------------------------------------------------------------------- #
class _ExplodingSession:
    def __init__(self):
        self.rolled_back = False

    def query(self, *a, **k):
        raise RuntimeError("database went away")

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        pass


def test_announce_invite_never_raises(monkeypatch):
    """The invitation row is already committed by the time this runs. A
    failure here must be logged and swallowed, not surfaced as a 500 that
    tempts the host into inviting the same clan twice."""
    logged = []
    monkeypatch.setattr(invites, "_log_failure", lambda where, e: logged.append(where))
    s = _ExplodingSession()
    result = invites.announce_invite(
        s, event=_event(), event_group=SimpleNamespace(id=3, group_id=42)
    )
    assert result is None
    assert s.rolled_back is True
    assert logged == ["announce_invite"]


def test_announce_response_never_raises(monkeypatch):
    logged = []
    monkeypatch.setattr(invites, "_log_failure", lambda where, e: logged.append(where))
    result = invites.announce_response(
        _ExplodingSession(),
        event=_event(),
        event_group=SimpleNamespace(id=3, group_id=42),
        accepted=True,
    )
    assert result is None
    assert logged == ["announce_response"]


def test_announce_withdrawal_never_raises(monkeypatch):
    logged = []
    monkeypatch.setattr(invites, "_log_failure", lambda where, e: logged.append(where))
    result = invites.announce_withdrawal(
        _ExplodingSession(),
        event=_event(),
        event_group=SimpleNamespace(id=3, group_id=42),
    )
    assert result is None
    assert logged == ["announce_withdrawal"]


# --------------------------------------------------------------------------- #
# Thread anchoring
# --------------------------------------------------------------------------- #
def test_thread_is_anchored_per_invited_clan(monkeypatch):
    """One thread per (event, invited clan) — NOT one per event. A 12-clan
    battle must be 12 private negotiations, not a room every rival can read."""
    captured = {}

    def _fake_get_or_create(s, **kw):
        captured.update(kw)
        return SimpleNamespace(id=99)

    import services.chat as chat

    monkeypatch.setattr(chat, "get_or_create_thread", _fake_get_or_create)

    invites.ensure_thread(
        None,
        event=_event(group_id=1),
        event_group=SimpleNamespace(id=55, group_id=42),
        host_group_name="Clan A",
        invited_group_name="Clan B",
    )
    assert captured["kind"] == invites.THREAD_KIND
    assert captured["subject_type"] == invites.SUBJECT_TYPE
    assert captured["subject_id"] == 55  # the web_event_groups row, not the event
    assert captured["parties"] == [("group", 1), ("group", 42)]
    assert captured["owner_party"] == ("group", 1)
    assert captured["title"] == "Clan A vs Clan B"


def test_self_hosted_edge_case_does_not_duplicate_the_party(monkeypatch):
    captured = {}
    import services.chat as chat

    monkeypatch.setattr(
        chat, "get_or_create_thread",
        lambda s, **kw: (captured.update(kw), SimpleNamespace(id=1))[1],
    )
    invites.ensure_thread(
        None,
        event=_event(group_id=42),
        event_group=SimpleNamespace(id=55, group_id=42),
    )
    assert captured["parties"] == [("group", 42)]
