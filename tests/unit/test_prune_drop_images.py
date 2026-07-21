"""Unit tests for the drop-image retention helper (scripts/prune_drop_images).

The high-risk surface is ``local_path_for``: it turns a DB-stored ``image_url``
into a path the script will ``os.remove``. A wrong answer here deletes the
wrong file, so the containment check gets the bulk of the coverage.

The module imports ``db.models.base`` at import time, which the conftest stubs;
loading it by file path under a throwaway name keeps that stub in play.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def prune():
    spec = importlib.util.spec_from_file_location(
        "_prune_drop_images_under_test", REPO_ROOT / "scripts" / "prune_drop_images.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


ROOT = "/store/droptracker/disc/static/assets/img/user-upload/"


# ── accepted forms ───────────────────────────────────────────────────────────

def test_canonical_public_url(prune):
    assert prune.local_path_for(
        "https://www.droptracker.io/img/user-upload/123/drop/Zulrah/Coal_0.jpg"
    ) == ROOT + "123/drop/Zulrah/Coal_0.jpg"


@pytest.mark.parametrize("prefix", [
    "https://www.droptracker.io/img/user-upload/",
    "https://droptracker.io/img/user-upload/",
    "http://www.droptracker.io/img/user-upload/",
    "http://droptracker.io/img/user-upload/",
])
def test_all_host_variants_resolve(prune, prefix):
    assert prune.local_path_for(prefix + "9/drop/N/x.png") == ROOT + "9/drop/N/x.png"


def test_historical_local_path_rows(prune):
    # Some rows stored the filesystem path instead of the URL.
    assert prune.local_path_for(ROOT + "5/drop/N/x.png") == ROOT + "5/drop/N/x.png"


def test_query_and_fragment_are_stripped(prune):
    assert prune.local_path_for(
        "https://www.droptracker.io/img/user-upload/1/drop/N/x.png?v=2#frag"
    ) == ROOT + "1/drop/N/x.png"


def test_surrounding_whitespace_tolerated(prune):
    assert prune.local_path_for(
        "  https://www.droptracker.io/img/user-upload/1/drop/N/x.png  "
    ) == ROOT + "1/drop/N/x.png"


# ── refused forms (must never yield a deletable path) ────────────────────────

@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_values_refused(prune, value):
    assert prune.local_path_for(value) is None


def test_foreign_host_refused(prune):
    assert prune.local_path_for("https://evil.example.com/img/user-upload/1/x.png") is None


def test_other_droptracker_paths_refused(prune):
    # Only the user-upload tree is ours to prune — not itemdb/clans/lootboards.
    assert prune.local_path_for("https://www.droptracker.io/img/itemdb/4151.png") is None
    assert prune.local_path_for("https://www.droptracker.io/img/clans/5/board.png") is None


def test_traversal_escaping_the_root_refused(prune):
    assert prune.local_path_for(
        "https://www.droptracker.io/img/user-upload/../../../../etc/passwd") is None


def test_traversal_that_stays_inside_is_allowed(prune):
    assert prune.local_path_for(
        "https://www.droptracker.io/img/user-upload/1/drop/../drop/N/x.png"
    ) == ROOT + "1/drop/N/x.png"


def test_bare_root_refused(prune):
    assert prune.local_path_for("https://www.droptracker.io/img/user-upload/") is None


def test_discord_cdn_url_refused(prune):
    # Older rows point at Discord's CDN; those are not ours to delete.
    assert prune.local_path_for(
        "https://cdn.discordapp.com/attachments/1/2/screenshot.png") is None


# ── byte formatting ──────────────────────────────────────────────────────────

def test_fmt_bytes(prune):
    assert prune._fmt_bytes(0) == "0.0 B"
    assert prune._fmt_bytes(1536) == "1.5 KiB"
    assert prune._fmt_bytes(5 * 1024 ** 3) == "5.0 GiB"


# ── retention policy boundaries (documents the SQL predicate) ────────────────

@pytest.mark.parametrize("value,quantity,kept", [
    (1_000_000, 1, True),    # exactly the threshold is KEPT (>=)
    (999_999, 1, False),
    (500_000, 2, True),      # value*quantity crosses it
    (500_000, 1, False),
    (153, 25, False),        # the real-world case: a stack of Coal
])
def test_total_value_threshold(value, quantity, kept):
    assert ((value * quantity) >= 1_000_000) is kept
