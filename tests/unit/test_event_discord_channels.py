"""Unit tests for PUT /events/{id}/discord channel-cache validation.

The bug being pinned: deleting a Discord channel drops it out of the bot's
channel cache, and the PUT used to re-validate EVERY submitted kind against
that cache — so the stale id in an untouched kind 422'd the whole save, and
the admin couldn't repoint or clear anything ("unknown channel" wedge).
The fix grandfathers ids already stored for the scope while the guild is
unchanged: only new/changed ids must exist in the (warm) cache.
"""

import pytest

from web_api.common import ProblemException
import web_api.routes.event_discord as ed


WARM_CACHE = [
    {"id": "100", "name": "general", "type": "text"},
    {"id": "200", "name": "events", "type": "text"},
    {"id": "300", "name": "help", "type": "forum"},
]


@pytest.fixture
def refresh_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(ed, "_request_channel_refresh", calls.append)
    return calls


@pytest.fixture
def warm_cache(monkeypatch):
    monkeypatch.setattr(ed, "_guild_channels", lambda gid: WARM_CACHE)


class TestValidateChannelsAgainstCache:
    def test_new_id_missing_from_warm_cache_rejected(self, warm_cache, refresh_calls):
        with pytest.raises(ProblemException) as exc:
            ed._validate_channels_against_cache(
                "1", {"announcements": "999"}, grandfathered=set())
        assert exc.value.status == 422
        # The bot is asked to re-fetch so a just-created channel works on retry.
        assert refresh_calls == ["1"]

    def test_grandfathered_stale_id_survives(self, warm_cache, refresh_calls):
        # "999" was deleted on Discord (not in the cache) but is already
        # stored for this scope — it must not block the save.
        ed._validate_channels_against_cache(
            "1", {"announcements": "999"}, grandfathered={"999"})
        assert refresh_calls == []

    def test_stale_untouched_kind_plus_new_valid_kind_saves(self, warm_cache):
        # The reported wedge: one kind repointed to a real channel while
        # another still holds the deleted channel's id.
        ed._validate_channels_against_cache(
            "1", {"announcements": "999", "completions": "100"},
            grandfathered={"999"})

    def test_new_valid_id_accepted(self, warm_cache, refresh_calls):
        ed._validate_channels_against_cache(
            "1", {"completions": "200"}, grandfathered=set())
        assert refresh_calls == []

    def test_new_forum_id_rejected(self, warm_cache):
        with pytest.raises(ProblemException) as exc:
            ed._validate_channels_against_cache(
                "1", {"announcements": "300"}, grandfathered=set())
        assert exc.value.status == 422

    def test_grandfathered_forum_id_not_rechecked(self, warm_cache):
        ed._validate_channels_against_cache(
            "1", {"announcements": "300"}, grandfathered={"300"})

    def test_cold_cache_never_blocks_but_requests_refresh(self, monkeypatch, refresh_calls):
        monkeypatch.setattr(ed, "_guild_channels", lambda gid: None)
        ed._validate_channels_against_cache(
            "1", {"announcements": "999"}, grandfathered=set())
        assert refresh_calls == ["1"]

    def test_all_grandfathered_skips_cache_read(self, monkeypatch, refresh_calls):
        def _boom(gid):
            raise AssertionError("cache should not be read")
        monkeypatch.setattr(ed, "_guild_channels", _boom)
        ed._validate_channels_against_cache(
            "1", {"announcements": "999", "leaderboard": "888"},
            grandfathered={"999", "888"})
        assert refresh_calls == []
