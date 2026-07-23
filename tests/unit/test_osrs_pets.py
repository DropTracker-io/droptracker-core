"""Unit tests for the pet taxonomy (utils/osrs_pets.py).

Pure leaf module (no db/service imports), so it loads for real under the
conftest stubs — the same way the engine and validator consume it.
"""
from __future__ import annotations

import utils.osrs_pets as pets


def test_any_pet_matches_boss_and_skilling():
    assert pets.pet_matches("Baby mole")          # boss
    assert pets.pet_matches("Beaver")             # skilling
    assert pets.pet_matches("Olmlet")             # raids


def test_any_pet_excludes_misc_by_default():
    # "misc" pets are opt-in — a bare "any pet" task must not count them.
    assert pets.pet_matches("Chompy chick") is False
    assert pets.pet_matches("Abyssal protector") is False


def test_category_gating():
    assert pets.pet_matches("Baby mole", ["boss"])
    assert pets.pet_matches("Baby mole", ["skilling"]) is False
    assert pets.pet_matches("Beaver", ["skilling"])
    assert pets.pet_matches("Beaver", ["boss"]) is False


def test_misc_matches_only_when_named():
    assert pets.pet_matches("Chompy chick", ["misc"])
    assert pets.pet_matches("Chompy chick", ["boss", "misc"])


def test_category_union():
    assert pets.pet_matches("Beaver", ["boss", "skilling"])


def test_normalization_is_case_and_space_insensitive():
    assert pets.pet_matches("  baby   MOLE ")
    assert pets.pet_matches("lil' zik", ["raids"])


def test_empty_and_unknown():
    assert pets.pet_matches("") is False
    assert pets.pet_matches(None) is False
    assert pets.pet_matches("Not a pet") is False
    assert pets.pet_matches("Baby mole", ["nonsense"]) is False


def test_is_known_pet_includes_misc():
    assert pets.is_known_pet("Baby mole")
    assert pets.is_known_pet("Chompy chick")      # misc still "known"
    assert pets.is_known_pet("Definitely not a pet") is False


def test_canonical_pet_name_snaps_spelling():
    assert pets.canonical_pet_name("baby mole") == "Baby mole"
    assert pets.canonical_pet_name("LIL' ZIK") == "Lil' zik"
    assert pets.canonical_pet_name("nope") is None


def test_pet_categories_includes_misc():
    keys = pets.pet_categories()
    assert "boss" in keys and "skilling" in keys and "raids" in keys and "misc" in keys


def test_all_pets_vs_every_pet():
    # ALL_PETS (default) omits misc; EVERY_PET includes it.
    assert pets._norm("Chompy chick") not in pets.ALL_PETS
    assert pets._norm("Chompy chick") in pets.EVERY_PET


def test_skilling_pet_source_maps_skill():
    assert pets.skilling_pet_source("Beaver") == "Woodcutting"
    assert pets.skilling_pet_source("Rock golem") == "Mining"
    assert pets.skilling_pet_source("baby chinchompa") == "Hunter"   # case-insensitive
    assert pets.skilling_pet_source("Quetzin") == "Hunter"
    assert pets.skilling_pet_source("Soup") == "Sailing"


def test_skilling_pet_source_none_for_non_skilling():
    # Boss pets and unknowns get no skill source (attributed via NPC instead).
    assert pets.skilling_pet_source("Baby mole") is None
    assert pets.skilling_pet_source("Not a pet") is None


def test_every_skilling_category_pet_has_a_source_except_herbi():
    # Guards against a new skilling pet landing in the taxonomy without a source
    # mapping. Herbi is intentionally excluded (plugin sends Herbiboar as an NPC).
    for norm_name in pets.PET_CATEGORIES["skilling"]:
        if norm_name == pets._norm("Herbi"):
            continue
        assert norm_name in pets.SKILLING_PET_SKILL, f"{norm_name} missing a skill source"
