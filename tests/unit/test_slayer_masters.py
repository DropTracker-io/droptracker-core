"""Registry invariants for utils/slayer_masters.py.

The ids are what event tasks store and what the matcher compares, so the
mapping the game cache confirms (slayer_master_task row names) and the
reset-master set the default exclusion is built from are pinned here.
"""
import pytest

from utils import slayer_masters as sm


def test_ids_are_unique_and_contiguous():
    assert [m.id for m in sm.SLAYER_MASTERS] == list(range(1, 11))


def test_ids_match_the_game_cache():
    # slayer_master_task (dbtable 114): turael_*, mazchna_*, ... mortimer_*.
    assert [sm.master_name(i) for i in range(1, 11)] == [
        "Turael", "Mazchna", "Vannaka", "Chaeldar", "Duradel",
        "Nieve", "Krystilia", "Konar", "Spria", "Mortimer",
    ]
    assert all(m.verified for m in sm.SLAYER_MASTERS)
    # 11 is the Leagues-only master; seasonal completions never reach events.
    assert sm.master_name(11) is None
    assert sm.master_name("garbage") is None


def test_reset_masters_are_turael_and_spria():
    assert sm.RESET_MASTER_IDS == frozenset({1, 9})
    assert sm.DEFAULT_EXCLUDED_MASTER_IDS == sm.RESET_MASTER_IDS
    for mid in sm.RESET_MASTER_IDS:
        master = sm.master_by_id(mid)
        assert master.resets_streak and not master.awards_points


def test_alternates_share_their_masters_slot():
    assert sm.master_by_name("aya").id == 1
    assert sm.master_by_name("Konar quo Maten").id == 8
    assert sm.master_by_name(" kuradal ").id == 5
    assert sm.master_by_name("Bob") is None
    assert sm.master_by_name(None) is None


def test_normalize_master_ids_accepts_ids_and_names_and_dedupes():
    assert sm.normalize_master_ids(["Duradel", 5, "5", "krystilia", "Kuradal"]) == [5, 7]
    assert sm.normalize_master_ids([]) == []
    assert sm.normalize_master_ids(None) == []


def test_normalize_master_ids_names_every_unknown_entry():
    with pytest.raises(ValueError) as exc:
        sm.normalize_master_ids(["Turael", "Bob", 42, True])
    message = str(exc.value)
    assert "Bob" in message and "42" in message and "True" in message
    assert "Turael" not in message


def test_canonical_task_name_folds_case_only():
    assert sm.canonical_task_name("cave KRAKEN") == "Cave kraken"
    assert sm.canonical_task_name("  the abyssal   sire ") == "The Abyssal Sire"
    assert sm.canonical_task_name("Dagannoth King") is None   # not fuzzy
    assert sm.canonical_task_name(None) is None


def test_normalize_task_names_dedupes_and_reports_unknown():
    assert sm.normalize_task_names(["abyssal demons", "Abyssal Demons", "Zulrah"]) == [
        "Abyssal demons", "Zulrah",
    ]
    with pytest.raises(ValueError) as exc:
        sm.normalize_task_names(["Zulrah", "Goblin", ""])
    assert "Goblin" in str(exc.value) and "(empty)" in str(exc.value)


def test_catalog_records_shape():
    records = sm.catalog_records()
    assert [r["id"] for r in records] == list(range(1, 11))
    turael = records[0]
    assert turael["name"] == "Turael" and turael["aliases"] == ["Aya"]
    assert turael["resets_streak"] is True and turael["awards_points"] is False
    assert len(sm.SLAYER_TASK_NAMES) == len(set(sm.SLAYER_TASK_NAMES))
