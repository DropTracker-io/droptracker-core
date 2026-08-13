"""Clan Log: claim folding, period windows, and the catalog name repair.

The three things that decide whether a board tells the truth:

* **Folding.** A month's cell names the *first* member to get the item, counts
  every occurrence, and lets a drop outrank a supplement at the same instant.
  Get this wrong and the board credits the wrong person, or double-counts a
  rebuild against a tail.
* **Period windows.** Month / year / all-time all read the same monthly ledger,
  so `period_contains` is the only thing keeping "what did we get in August"
  apart from "what have we ever got".
* **Name repair.** The curated sheet's shorthand is correct in context and
  wrong out of it — under the Ahrim header "Staff" means Ahrim's staff, and the
  `items` table has a plain item called "Staff" waiting to be picked. This is
  the guard that turned 40 mis-resolved rows into 0.

Loaded from file paths so the conftest ``services`` stub doesn't shadow them.
"""

import importlib.util
import os
import sys
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, *parts: str):
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_with_stubs(name: str, parts: tuple, stubs: dict):
    """Load a module that imports from ``db.models.*``, then put sys.modules back.

    The conftest stubs ``db.models`` as a plain MagicMock, so it is not a
    package and ``db.models.clan_log`` cannot be imported from it. Injecting
    lightweight stand-ins is enough for the pure functions under test — but they
    MUST be removed again: leaving a fake ``db.models.recap`` behind breaks
    every later test that imports the real one, which is the same sys.modules
    poisoning that already cost this suite its green baseline once.
    """
    import types

    saved = {key: sys.modules.get(key) for key in stubs}
    try:
        for key, attrs in stubs.items():
            module = types.ModuleType(key)
            for attr, value in attrs.items():
                setattr(module, attr, value)
            sys.modules[key] = module
        return _load(name, *parts)
    finally:
        for key, original in saved.items():
            if original is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = original


clan_log = _load_with_stubs(
    "_clan_log_under_test",
    ("services", "clan_log.py"),
    {
        "db.models.clan_log": {
            "CLAN_LOG_SCHEMA_VERSION": 1,
            "PERIOD_ALL": "all",
            "SCOPE_CLAN_LOG": "clan_log",
            "SOURCE_DROP": "drop",
            "SOURCE_CLOG": "clog",
            "SOURCE_PET": "pet",
        },
        "db.models.recap": {"RecapSnapshot": object},
    },
)
catalog_source = _load(
    "_clan_log_catalog_under_test", "scripts", "clan_log", "catalog_source.py"
)


def _claim(item_id, player_id, when, source=None, ref_id=None, proof=None):
    return {
        "item_id": item_id,
        "player_id": player_id,
        "at": when,
        "source": source or clan_log.SOURCE_DROP,
        "ref_id": ref_id,
        "proof": proof,
    }


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "period,ok",
    [
        ("all", True), ("2026", True), ("2026-08", True), ("2026-12", True),
        ("2026-13", False), ("2026-00", False), ("26-08", False),
        ("", False), ("august", False), ("2026-8", False),
    ],
)
def test_is_valid_period(period, ok):
    assert clan_log.is_valid_period(period) is ok


def test_period_contains_windows():
    # All-time takes everything; a year takes its months; a month takes itself.
    assert clan_log.period_contains("all", "2024-01")
    assert clan_log.period_contains("2026", "2026-08")
    assert not clan_log.period_contains("2026", "2025-12")
    assert clan_log.period_contains("2026-08", "2026-08")
    assert not clan_log.period_contains("2026-08", "2026-07")


def test_current_periods_are_all_year_month():
    periods = clan_log.current_periods(datetime(2026, 8, 13))
    assert periods == ["all", "2026", "2026-08"]


# --------------------------------------------------------------------------- #
# Folding claims into the monthly ledger
# --------------------------------------------------------------------------- #
def test_fold_splits_by_month_and_keeps_first_per_month():
    canonical = {100: 100}
    folded = clan_log.fold_claims(
        [
            _claim(100, 7, datetime(2026, 8, 20)),
            _claim(100, 3, datetime(2026, 8, 2)),   # earliest in August
            _claim(100, 9, datetime(2026, 7, 30)),  # different month = own row
        ],
        canonical,
    )
    assert set(folded) == {(100, "2026-08"), (100, "2026-07")}
    assert folded[(100, "2026-08")]["player_id"] == 3
    assert folded[(100, "2026-08")]["obtained_count"] == 2
    assert folded[(100, "2026-08")]["player_count"] == 2
    assert folded[(100, "2026-07")]["player_id"] == 9


def test_fold_prefers_a_drop_over_a_supplement_at_the_same_instant():
    """A clog unlock and its drop can land on the same timestamp. The drop is
    the evidence, so it must own the row (and carry its drop_id)."""
    when = datetime(2026, 8, 5, 12, 0, 0)
    folded = clan_log.fold_claims(
        [
            _claim(100, 5, when, source=clan_log.SOURCE_CLOG),
            _claim(100, 6, when, source=clan_log.SOURCE_DROP, ref_id=4242),
        ],
        {100: 100},
    )
    row = folded[(100, "2026-08")]
    assert row["source"] == clan_log.SOURCE_DROP
    assert row["player_id"] == 6
    assert row["drop_id"] == 4242


def test_fold_maps_variants_onto_the_canonical_slot():
    """An uncharged Scythe and a charged one are one board cell, not two."""
    folded = clan_log.fold_claims(
        [
            _claim(222, 1, datetime(2026, 8, 1)),   # variant id
            _claim(111, 2, datetime(2026, 8, 4)),   # canonical id
        ],
        {111: 111, 222: 111},
    )
    assert set(folded) == {(111, "2026-08")}
    assert folded[(111, "2026-08")]["obtained_count"] == 2
    assert folded[(111, "2026-08")]["player_id"] == 1


def test_fold_ignores_items_outside_the_catalog():
    assert clan_log.fold_claims([_claim(999, 1, datetime(2026, 8, 1))], {100: 100}) == {}


def test_fold_only_drops_carry_a_drop_id():
    folded = clan_log.fold_claims(
        [_claim(100, 1, datetime(2026, 8, 1), source=clan_log.SOURCE_PET, ref_id=55)],
        {100: 100},
    )
    assert folded[(100, "2026-08")]["drop_id"] is None


# --------------------------------------------------------------------------- #
# Proof URLs
# --------------------------------------------------------------------------- #
def test_clean_proof_maps_on_disk_paths_and_drops_junk():
    # `drops.image_url` is not reliably a URL — some rows hold the disk path.
    assert clan_log._clean_proof(
        "/store/droptracker/disc/static/assets/img/user-upload/x.png"
    ) == "https://www.droptracker.io/img/user-upload/x.png"
    assert clan_log._clean_proof("https://example.com/a.png") == "https://example.com/a.png"
    assert clan_log._clean_proof("some-local-file.png") is None
    assert clan_log._clean_proof(None) is None


# --------------------------------------------------------------------------- #
# Catalog name repair
# --------------------------------------------------------------------------- #
_BARROWS = {
    4708: "Ahrim's hood", 4710: "Ahrim's staff",
    1169: "Coif", 4732: "Karil's coif", 4734: "Karil's crossbow",
    4747: "Torag's hammers",
}


def test_repair_rewrites_shorthand_to_the_bosss_own_item():
    # "Staff" resolves to the plain item 1379; the boss's table says otherwise.
    item_id, note = catalog_source.repair_item_id("Staff", 1379, _BARROWS)
    assert item_id == 4710
    assert "repaired" in note


def test_repair_handles_singular_shorthand_for_a_plural_drop():
    item_id, _ = catalog_source.repair_item_id("Hammer", 2347, _BARROWS)
    assert item_id == 4747


def test_repair_falls_back_to_the_distinctive_first_word():
    """The sheet names the finished item where the boss drops a component."""
    table = {13204: "Pegasian crystal", 13202: "Primordial crystal"}
    item_id, note = catalog_source.repair_item_id("Pegasian boots", 13237, table)
    assert item_id == 13204
    assert "repaired" in note


def test_repair_leaves_an_ambiguous_name_alone_and_says_so():
    table = {1: "Noxious point", 2: "Noxious blade", 3: "Noxious pommel"}
    item_id, note = catalog_source.repair_item_id("Noxious halberd", 29796, table)
    assert item_id == 29796
    assert "ambiguous" in note


def test_repair_accepts_a_clean_resolution_silently():
    item_id, note = catalog_source.repair_item_id("Ahrim's hood", 4708, _BARROWS)
    assert item_id == 4708
    assert note is None


def test_repair_without_a_wiki_table_changes_nothing():
    """No coverage is not evidence of a bad name — new content has no rows."""
    item_id, note = catalog_source.repair_item_id("Yama's unique", 5000, {})
    assert item_id == 5000
    assert note is None


def test_slug_and_variant_helpers():
    assert catalog_source.slugify("Vet'ion / Calvar'ion") == "vet_ion_calvar_ion"
    assert catalog_source.base_item_name("Scythe of vitur (uncharged)") == "Scythe of vitur"
    assert catalog_source.base_item_name("Twisted bow") == "Twisted bow"


def test_sections_from_template_flattens_groups_into_board_rows():
    template = {
        "tasks": [
            {
                "label": "Barrows Brothers",
                "config": {
                    "groups": [
                        {"label": "Ahrim", "npcs": ["Ahrim the Blighted"],
                         "items": [{"item_name": "Ahrim's hood"}]},
                        {"label": "Dharok", "npcs": ["Dharok the Wretched"],
                         "items": [{"item_name": "Dharok's helm"}]},
                    ]
                },
            },
            # A group with no items contributes no board row.
            {"label": "Empty", "config": {"groups": [{"label": "Empty", "items": []}]}},
        ]
    }
    sections = catalog_source.sections_from_template(template)
    assert [s["label"] for s in sections] == ["Ahrim", "Dharok"]
    assert sections[0]["category"] == "group_bosses"
    assert sections[0]["slug"] == "barrows_brothers_ahrim"
