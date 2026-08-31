"""The avatar crop: where it is stored, and how a list of players finds one.

The interesting cases are all about *absence*. Most players have never uploaded
a model, a render can lag its fingerprint by seconds, and a pruned outfit leaves
a fingerprint pointing at nothing — so the batch lookup has to answer "no
avatar" cheaply and often, and must never be the reason a leaderboard fails.
"""
import sys
from contextlib import contextmanager

import pytest

import web_api.common as common
from services.gear_image import (
    _page_url,
    _ready_js,
    avatar_path,
    avatar_url,
    image_path,
    image_url,
)
from web_api.common import _cache, cache_set, player_avatars

# `services` is a MagicMock in the test bootstrap, so `import services.gear_image`
# would bind a mock child rather than the module conftest loaded from disk.
# Patching that would patch nothing, silently.
gear_image = sys.modules["services.gear_image"]


class TestAvatarPaths:
    def test_avatar_is_a_different_file_from_the_still(self):
        """Both are PNGs for one outfit; a shared name would overwrite one."""
        assert avatar_path(7, "abc123") != image_path(7, "abc123")
        assert avatar_url(7, "abc123") != image_url(7, "abc123")

    def test_avatar_name_carries_the_fingerprint(self):
        # The fingerprint in the name is what makes an unchanged outfit free.
        assert avatar_path(7, "abc123").endswith("/7/abc123-avatar.png")
        assert avatar_url(7, "abc123").endswith("/7/abc123-avatar.png")

    def test_player_id_is_coerced_to_an_int(self):
        """It reaches a filesystem path, so a string must not pass through."""
        assert avatar_path("7", "abc123") == avatar_path(7, "abc123")


class TestRenderPageUrl:
    def test_avatar_variant_asks_for_the_bust(self):
        assert "avatar=1" in _page_url(7, "abc123", avatar=True)

    def test_avatar_never_asks_for_a_pet(self):
        """The pet stands beside the player, outside a head-and-shoulders crop."""
        url = _page_url(7, "abc123", with_pet=True, avatar=True)
        assert "pet=1" not in url

    def test_still_still_asks_for_the_pet(self):
        assert "pet=1" in _page_url(7, "abc123", with_pet=True)

    def test_ready_probe_names_the_framing(self):
        """A server on an older build renders full-body for `?avatar=1`; the
        probe is what stops that being stored as the avatar."""
        assert "'bust'" in _ready_js("bust")
        assert "'full'" in _ready_js("full")


class TestPlayerAvatars:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _cache.clear()
        yield
        _cache.clear()

    def test_no_ids_does_no_work(self):
        assert player_avatars([]) == {}

    def test_reads_cached_hits_without_touching_the_database(self):
        cache_set("avatar:5", "https://example.test/5.png")
        assert player_avatars([5]) == {5: "https://example.test/5.png"}

    def test_a_cached_miss_stays_a_miss(self):
        """Players without a model are cached as "" — the common case, and the
        one the cache exists to keep off the database."""
        cache_set("avatar:6", "")
        assert player_avatars([6]) == {}

    def test_mixes_hits_and_misses(self):
        cache_set("avatar:5", "https://example.test/5.png")
        cache_set("avatar:6", "")
        assert player_avatars([5, 6]) == {5: "https://example.test/5.png"}

    def test_ignores_none_ids(self):
        """Event rows carry a null player_id for privacy-masked players."""
        assert player_avatars([None]) == {}

    def test_accepts_a_generator(self):
        cache_set("avatar:5", "https://example.test/5.png")
        assert player_avatars(pid for pid in (5,)) == {5: "https://example.test/5.png"}


class TestPlayerAvatarsAgainstTheDatabase:
    """The uncached path: fingerprint from the row, file from the disk."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _cache.clear()
        yield
        _cache.clear()

    @pytest.fixture
    def rows(self, monkeypatch):
        """Stubs the state query; the test sets what it returns."""
        held = []

        class _Session:
            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def all(self):
                return held

        @contextmanager
        def _db_session():
            yield _Session()

        monkeypatch.setattr(common, "db_session", _db_session)
        return held

    def test_a_rendered_outfit_becomes_a_url(self, rows, monkeypatch):
        rows.append((5, "abc123"))
        monkeypatch.setattr(gear_image, "avatar_exists", lambda pid, fp: True)
        assert player_avatars([5]) == {5: avatar_url(5, "abc123")}

    def test_a_fingerprint_with_no_rendered_file_is_not_a_url(self, rows, monkeypatch):
        """A render lags its fingerprint by seconds, and a prune can outlive
        one entirely — neither may put a broken image on a leaderboard."""
        rows.append((5, "abc123"))
        monkeypatch.setattr(gear_image, "avatar_exists", lambda pid, fp: False)
        assert player_avatars([5]) == {}

    def test_a_player_with_no_row_is_a_cached_miss(self, rows):
        assert player_avatars([5]) == {}
        # Cached as "" so the next page does not ask the database again.
        assert _cache["avatar:5"][1] == ""

    def test_a_database_failure_costs_the_caller_nothing(self, monkeypatch):
        """An avatar is decoration; it must never fail the list it decorates."""
        @contextmanager
        def _explode():
            raise RuntimeError("database is down")
            yield  # pragma: no cover

        monkeypatch.setattr(common, "db_session", _explode)
        assert player_avatars([5]) == {}
