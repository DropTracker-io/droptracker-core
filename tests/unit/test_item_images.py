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
