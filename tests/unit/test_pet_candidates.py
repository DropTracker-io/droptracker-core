"""Plugin 6.1.24 without the manifest sends the item lines it saw as
``pet_candidates``; the server picks the one that is a pet."""
from __future__ import annotations

import data.submissions.pet as pet
from utils.osrs_pets import canonical_pet_name


def test_the_candidate_that_is_a_pet_names_it():
    assert pet.pet_from_candidates(
        "Unknown Pet", "Clue scroll (elite)|Ikkle hydra") == "Ikkle hydra"


def test_a_known_name_is_kept_over_candidates():
    assert pet.pet_from_candidates("Heron", "Ikkle hydra") == "Heron"


def test_no_pet_among_candidates_stays_unknown():
    assert pet.pet_from_candidates("Unknown Pet", "Clue scroll (elite)") == "Unknown Pet"
    assert pet.pet_from_candidates("Unknown Pet", None) == "Unknown Pet"


def test_the_fractured_archive_pet_is_catalogued():
    assert canonical_pet_name("orion") == "Orion"
