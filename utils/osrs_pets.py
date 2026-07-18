# utils/osrs_pets.py — single source of truth for OSRS pet taxonomy.
#
# Consumed in two places:
#   * services/event_engine.match_task() — resolves "any pet" / "any boss pet"
#     etc. category tasks at match time (dynamic: a pet added here counts for
#     existing tasks with no re-seed).
#   * web_api/routes/event_task_validation — validates the category keys / a
#     specific-pet target when a pet_collection task is created or edited.
#
# This is a pure leaf module on purpose: no db/service imports (the test suite
# stubs db.models as a MagicMock, so importing _norm from there would silently
# break normalization). It carries its own _norm, identical to the engine's.


def _norm(value) -> str:
    """Case-insensitive / whitespace-collapsing name key — must stay identical
    to services.event_engine._norm so envelope pet names line up with the sets
    below."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


# Authored taxonomy (display spellings). Keep names as RuneLite reports the pet
# — the plugin's pet submission's ``pet_name`` is matched against these after
# _norm, so punctuation/casing here only needs to match the in-game name.
_CATEGORIES = {
    "boss": {"Abyssal orphan", "Baby mole", "Baron", "Bran",
             "Beef", "Butch", "Callisto cub", "Dom", "Gull", "Hellpuppy",
             "Huberte", "Ikkle hydra", "Jal-nib-rek", "Kalphite princess",
             "Lil' zik", "Lil'viathan", "Little nightmare", "Maggot marquess",
             "Moxi", "Muphin", "Nexling", "Nid", "Noon", "Olmlet", "Pet chaos elemental",
             "Pet dagannoth prime", "Pet dagannoth rex", "Pet dagannoth supreme", "Pet dark core",
             "Pet general graardor", "Pet k'ril tsutsaroth", "Pet kraken", "Pet kree'arra",
             "Pet smoke devil", "Pet snakeling", "Pet zilyana", "Phoenix", "Prince black dragon",
             "Scorpia's offspring", "Scurry", "Skotos", "Smolcano", "Smol heredit", "Sraracha",
             "Tiny tempor", "Tumeken's guardian", "Tzrek-jad", "Venenatis spiderling",
             "Vet'ion jr.", "Vorki", "Wisp", "Yami", "Youngllef"},
    "skilling": {"Baby chinchompa", "Beaver", "Giant squirrel", "Heron",
                 "Rift guardian", "Rock golem", "Rocky", "Soup", "Tangleroot",
                 "Quetzin", "Herbi"},
    "raids": {"Olmlet", "Tumeken's guardian", "Lil' zik"},
    # "misc": stackable / trivially-obtained pets. Excluded from the default
    # "any pet" set (DEFAULT_CATEGORIES) — a task must ask for the "misc"
    # category explicitly, or target one of these pets by name, for it to count.
    "misc": {"Abyssal protector", "Chompy chick"},
}

# Category keys that count toward a bare "any pet" task. Misc is opt-in only.
DEFAULT_EXCLUDED_CATEGORIES = frozenset({"misc"})
DEFAULT_CATEGORIES = tuple(k for k in _CATEGORIES if k not in DEFAULT_EXCLUDED_CATEGORIES)

# Precompute normalized sets once at import.
PET_CATEGORIES = {k: frozenset(_norm(n) for n in v) for k, v in _CATEGORIES.items()}

# "any pet" default set (misc excluded); EVERY_PET includes misc — used to
# validate a specific-pet target (a user may still pick a misc pet by name).
ALL_PETS = frozenset().union(*(PET_CATEGORIES[k] for k in DEFAULT_CATEGORIES))
EVERY_PET = frozenset().union(*PET_CATEGORIES.values())

# Normalized name -> display spelling, so validation can echo the canonical
# in-game name back onto the task's ``target``.
PET_DISPLAY_BY_NORM = {_norm(n): n for cat in _CATEGORIES.values() for n in cat}


def pet_categories() -> tuple[str, ...]:
    """All selectable category keys (includes ``misc`` — a task may opt in)."""
    return tuple(_CATEGORIES.keys())


def pets_in_category(cat: str) -> frozenset[str]:
    """Normalized pet-name set for a category (empty for an unknown key)."""
    return PET_CATEGORIES.get(cat, frozenset())


def is_known_pet(pet_name: str) -> bool:
    """True if ``pet_name`` is any catalogued pet (misc included) — the check
    behind a specific-pet task target."""
    return _norm(pet_name) in EVERY_PET


def canonical_pet_name(pet_name: str):
    """Display spelling for ``pet_name``, or None when it isn't a known pet."""
    return PET_DISPLAY_BY_NORM.get(_norm(pet_name))


def pet_matches(pet_name: str, categories=None) -> bool:
    """Does ``pet_name`` satisfy a pet task?

    ``categories`` None/empty -> "any pet" (the default set, misc excluded).
    Otherwise the pet must fall in at least one of the named categories
    (pass ``["misc"]`` — or include it — to count the opt-in ones).
    """
    n = _norm(pet_name)
    if not n:
        return False
    if not categories:
        return n in ALL_PETS
    return any(n in PET_CATEGORIES.get(c, frozenset()) for c in categories)
