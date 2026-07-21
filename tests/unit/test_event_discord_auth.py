"""Unit tests for the event-Discord guild-targeting authorization gate.

The security fix: an event admin could previously point an event's Discord
notifications at ANY guild the bot is in (the dropdown listed them all, and
the PUT never checked authority over the chosen guild). These tests pin the
new gate — a guild is targetable only when the user manages it on Discord
(``manageable_guild_ids``) or administers the DropTracker group linked to it.

The conftest stubs ``db`` and ``utils.redis``, so the DB session and the
Redis-backed ``manageable_guild_ids`` are faked here.
"""

import pytest

from web_api.common import ProblemException
import web_api.routes.event_discord as ed


class _FakeQuery:
    def __init__(self, results):
        self._results = results

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._results


class _FakeSession:
    """Returns queued ``.all()`` results in order — first the GroupAdmin
    grant query, then (only if grants exist) the Group.guild_id query."""

    def __init__(self, *result_batches):
        self._batches = list(result_batches)

    def query(self, *a, **k):
        return _FakeQuery(self._batches.pop(0) if self._batches else [])


@pytest.fixture
def patch_user(monkeypatch):
    """Patch load_user/is_superadmin/manageable_guild_ids on the module."""
    def _apply(*, superadmin=False, manage=frozenset(), manager_gids=frozenset()):
        monkeypatch.setattr(ed, "load_user", lambda s, uid: object())
        monkeypatch.setattr(ed, "is_superadmin", lambda user: superadmin)
        monkeypatch.setattr(ed, "manageable_guild_ids", lambda uid: set(manage))
        # web64a: _targetable_guild_ids also unions in event-manager groups; stub
        # it to an empty set by default so the scripted session stays aligned.
        monkeypatch.setattr(ed, "event_manager_group_ids",
                            lambda s, uid: set(manager_gids))
    return _apply


class TestTargetableGuildIds:
    def test_discord_managed_guild_is_targetable(self, patch_user):
        patch_user(manage={"111"})
        allowed = ed._targetable_guild_ids(_FakeSession([]), 7)
        assert allowed == {"111"}

    def test_admin_group_home_guild_added_when_oauth_cache_cold(self, patch_user):
        # No Discord-manage signal (cold OAuth cache), but the user has a web
        # grant on group 5 whose linked guild is 222.
        patch_user(manage=set())
        s = _FakeSession([(5,)], [("222",)])
        allowed = ed._targetable_guild_ids(s, 7)
        assert allowed == {"222"}

    def test_union_of_both_signals(self, patch_user):
        patch_user(manage={"111"})
        s = _FakeSession([(5,)], [("222",)])
        assert ed._targetable_guild_ids(s, 7) == {"111", "222"}

    def test_superadmin_returns_none_meaning_all(self, patch_user):
        patch_user(superadmin=True)
        assert ed._targetable_guild_ids(_FakeSession(), 7) is None

    def test_no_signals_means_empty(self, patch_user):
        patch_user(manage=set())
        assert ed._targetable_guild_ids(_FakeSession([]), 7) == set()


class TestAssertCanTargetGuild:
    def test_allows_targetable_guild(self, monkeypatch):
        monkeypatch.setattr(ed, "_targetable_guild_ids", lambda s, uid: {"111"})
        ed._assert_can_target_guild(_FakeSession(), 7, "111")  # no raise

    def test_rejects_unauthorized_guild(self, monkeypatch):
        monkeypatch.setattr(ed, "_targetable_guild_ids", lambda s, uid: {"111"})
        with pytest.raises(ProblemException) as exc:
            ed._assert_can_target_guild(_FakeSession(), 7, "999")
        assert exc.value.status == 403

    def test_superadmin_targets_any_guild(self, monkeypatch):
        monkeypatch.setattr(ed, "_targetable_guild_ids", lambda s, uid: None)
        ed._assert_can_target_guild(_FakeSession(), 7, "999")  # no raise
