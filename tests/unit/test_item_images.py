"""Item icon caching — `utils/item_images.py`.

The failure this module guards against is a silent one. Every caller treats a
missing icon as soft (the site falls back to a placeholder), so when the icon
directory was not writable by the account the bots run as, nothing errored —
items simply rendered as the placeholder GIF forever. These tests pin the two
behaviours that make that diagnosable and recoverable: the directory is created
world-writable because two service accounts write it, and a write failure is
logged rather than swallowed.
"""
import asyncio
import os
import stat
from unittest.mock import patch

import pytest

from utils import item_images


class TestEnsurePublicDir:
    def test_creates_directory_writable_by_both_service_accounts(self, tmp_path):
        target = tmp_path / "itemdb"
        item_images.ensure_public_dir(str(target))
        assert target.is_dir()
        mode = stat.S_IMODE(os.stat(target).st_mode)
        # The bots run as `user` and the backfills as `debian`; anything less
        # than group+other write reintroduces the original bug.
        assert mode & stat.S_IWGRP
        assert mode & stat.S_IWOTH

    def test_is_idempotent_on_an_existing_directory(self, tmp_path):
        target = tmp_path / "itemdb"
        target.mkdir()
        item_images.ensure_public_dir(str(target))
        item_images.ensure_public_dir(str(target))
        assert target.is_dir()

    def test_does_not_raise_when_chmod_is_refused(self, tmp_path):
        # A directory owned by the other account is already correct; failing to
        # chmod it must not abort the caller.
        target = tmp_path / "itemdb"
        with patch("os.chmod", side_effect=PermissionError("not owner")):
            item_images.ensure_public_dir(str(target))
        assert target.is_dir()


class TestEnsureItemImages:
    def test_returns_zero_and_makes_no_calls_when_all_icons_exist(self, tmp_path):
        for iid in (1, 2, 3):
            (tmp_path / f"{iid}.png").write_bytes(b"x")
        with patch.object(item_images, "ITEMDB_DIR", str(tmp_path)):
            with patch.object(item_images, "ensure_item_image") as fetch:
                fetched = asyncio.run(item_images.ensure_item_images([1, 2, 3]))
        assert fetched == 0
        # The steady-state ingest path must cost stat() calls and nothing else.
        fetch.assert_not_called()

    def test_ignores_unusable_input_rather_than_raising(self):
        assert asyncio.run(item_images.ensure_item_images([])) == 0
        assert asyncio.run(item_images.ensure_item_images(["not-an-id"])) == 0
        assert asyncio.run(item_images.ensure_item_images(None)) == 0

    def test_skips_negative_ids(self, tmp_path):
        # -1 is the game's "no item" sentinel; RuneLite has no icon for it.
        with patch.object(item_images, "ITEMDB_DIR", str(tmp_path)):
            with patch.object(item_images, "ensure_item_image") as fetch:
                assert asyncio.run(item_images.ensure_item_images([-1])) == 0
        fetch.assert_not_called()


class TestEnsureItemImageWriteFailure:
    @pytest.mark.parametrize("iid", [4151])
    def test_logs_rather_than_silently_swallowing_an_unwritable_directory(
        self, tmp_path, caplog, iid
    ):
        """The exact regression: PermissionError caught by a bare except.

        A missing icon is a soft failure everywhere downstream, so if this is
        not logged there is no signal anywhere that the cache has stopped
        filling.
        """

        class _Response:
            status = 200

            async def read(self):
                return b"png-bytes"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            def get(self, url):
                return _Response()

        with patch.object(item_images, "ITEMDB_DIR", str(tmp_path)):
            with patch("builtins.open", side_effect=PermissionError("denied")):
                with caplog.at_level("WARNING", logger=item_images.__name__):
                    ok = asyncio.run(
                        item_images.ensure_item_image(iid, session=_Session())
                    )

        assert ok is False
        assert any("Could not write item icon" in r.message for r in caplog.records)


class TestPlaceholder:
    """The bytes served when an icon cannot be produced.

    The old placeholder was a 672 KB animated logo returned at HTTP 200, which
    is two bugs in one: every item surface rendered a branded blob where a
    ~400-byte sprite belonged, and the success status meant the frontend could
    never tell a real icon from a failure.
    """

    def test_placeholder_is_a_tiny_transparent_png(self):
        data = item_images.TRANSPARENT_PNG
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        # Two orders of magnitude smaller than the GIF it replaces; the exact
        # size matters less than it staying negligible.
        assert len(data) < 200

    def test_placeholder_is_one_by_one(self):
        # The frontend identifies the fallback by intrinsic size (an <img> can
        # read neither the status code nor a header), and every real OSRS item
        # sprite is 36x32 — so 1x1 must stay 1x1.
        width = int.from_bytes(item_images.TRANSPARENT_PNG[16:20], "big")
        height = int.from_bytes(item_images.TRANSPARENT_PNG[20:24], "big")
        assert (width, height) == (1, 1)

    def test_failures_are_never_cached(self):
        """Cloudflare held the old placeholder for a day past the origin fix.

        A positive max-age is not sufficient: the zone's Browser Cache TTL
        rewrites any positive value upward (measured: 60 -> 1800), so the only
        directive that survives intact is no-store.
        """
        assert "no-store" in item_images.PLACEHOLDER_CACHE_CONTROL
        assert "max-age" not in item_images.PLACEHOLDER_CACHE_CONTROL


class TestNegativeCache:
    def setup_method(self):
        item_images._negative_cache.clear()

    teardown_method = setup_method

    def _session_returning(self, status):
        class _Response:
            def __init__(self):
                self.status = status

            async def read(self):
                return b""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            calls = 0

            def get(self_inner, url):
                _Session.calls += 1
                return _Response()

        return _Session()

    def test_a_404_stops_us_asking_again(self, tmp_path):
        """Repeated misses must be cheap.

        Without this, every page view of an item RuneLite has no icon for costs
        an outbound HTTPS round trip before we can answer — once per tile, per
        view.
        """
        session = self._session_returning(404)
        with patch.object(item_images, "ITEMDB_DIR", str(tmp_path)):
            for _ in range(5):
                asyncio.run(item_images.ensure_item_image(4151, session=session))
        assert type(session).calls == 1

    def test_a_server_error_is_retried(self, tmp_path):
        # A 5xx is RuneLite having a bad moment, not a verdict; caching it would
        # blank a perfectly good icon for the whole TTL.
        session = self._session_returning(503)
        with patch.object(item_images, "ITEMDB_DIR", str(tmp_path)):
            for _ in range(3):
                asyncio.run(item_images.ensure_item_image(4151, session=session))
        assert type(session).calls == 3

    def test_an_existing_file_short_circuits_before_the_cache(self, tmp_path):
        # Ordering matters: another process (the sweep) may have written the
        # icon since we cached the miss, and the file on disk always wins.
        item_images._remember_missing(4151)
        (tmp_path / "4151.png").write_bytes(b"x")
        with patch.object(item_images, "ITEMDB_DIR", str(tmp_path)):
            ok = asyncio.run(item_images.ensure_item_image(4151))
        assert ok is True

    def test_the_cache_is_bounded(self, tmp_path):
        # Keyed by anything a client can name, in a process that runs for
        # months — unbounded growth would be a slow leak.
        for iid in range(item_images._MAX_NEGATIVE_ENTRIES + 10):
            item_images._remember_missing(iid)
        assert len(item_images._negative_cache) <= item_images._MAX_NEGATIVE_ENTRIES
