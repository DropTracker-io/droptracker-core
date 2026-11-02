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
    "boss": {"Abyssal orphan", "Aggy", "Baby mole", "Baron", "Bran",
             "Beef", "Butch", "Callisto cub", "Dom", "Gull", "Hellpuppy",
             "Huberte", "Ikkle hydra", "Jal-nib-rek", "Kalphite princess",
             "Lil' zik", "Lil'viathan", "Little nightmare", "Maggot marquess",
             "Moxi", "Muphin", "Nexling", "Nid", "Noon", "Olmlet", "Orion", "Pet chaos elemental",
             "Pet dagannoth prime", "Pet dagannoth rex", "Pet dagannoth supreme", "Pet dark core",
             "Pet general graardor", "Pet k'ril tsutsaroth", "Pet kraken", "Pet kree'arra",
             "Pet smoke devil", "Pet snakeling", "Pet zilyana", "Phoenix", "Prince black dragon",
             "Scorpia's offspring", "Scurry", "Skotos", "Smolcano", "Smol heredit", "Sraracha",
             "Tiny tempor", "Tumeken's guardian", "Tzrek-jad", "Venenatis spiderling",
             "Vet'ion jr.", "Vorki", "Wisp", "Yami", "Youngllef"},
    "skilling": {"Baby chinchompa", "Beaver", "Giant squirrel", "Heron",
                 "Rift guardian", "Rock golem", "Rocky", "Soup", "Tangleroot",
                 "Quetzin", "Herbi", "Mr mcgroot"},
    "raids": {"Olmlet", "Tumeken's guardian", "Lil' zik", "Orion"},
    # Clue-scroll and minigame pets: rare grinds like any boss/skilling pet, so
    # they count toward a bare "any pet" task — they just have no NPC to kill.
    "clue": {"Bloodhound"},
    "minigame": {"Lil' creator", "Pet penance queen"},
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


# Skilling pets -> the skill they're obtained from. Used to attribute a pet's
# ``source`` server-side: skilling pets have no NPC/boss, and the plugin does not
# send a source for them. This is a display string, NOT an NpcList entry, so it
# must never be run through NPC resolution. Keeping it here (the pet SSOT) means a
# new skilling pet needs one backend edit, not a plugin release.
_SKILLING_PET_SKILL = {
    "Baby chinchompa": "Hunter",
    "Beaver": "Woodcutting",
    "Giant squirrel": "Agility",
    "Heron": "Fishing",
    "Quetzin": "Hunter",
    "Rift guardian": "Runecraft",
    "Rock golem": "Mining",
    "Rocky": "Thieving",
    "Soup": "Sailing",
    "Tangleroot": "Farming",
    "Mr mcgroot": "Hunter",
}
SKILLING_PET_SKILL = {_norm(k): v for k, v in _SKILLING_PET_SKILL.items()}


def skilling_pet_source(pet_name: str):
    """Skill a skilling pet is obtained from (display string), or None if the pet
    isn't a mapped skilling pet. Not an NPC — do not resolve against ``npc_list``."""
    return SKILLING_PET_SKILL.get(_norm(pet_name))


def pet_categories() -> tuple[str, ...]:
    """All selectable category keys (includes ``misc`` — a task may opt in)."""
    return tuple(_CATEGORIES.keys())


def pets_in_category(cat: str) -> frozenset[str]:
    """Normalized pet-name set for a category (empty for an unknown key)."""
    return PET_CATEGORIES.get(cat, frozenset())


def eligible_pet_names(categories=None) -> tuple[str, ...]:
    """Display spellings of every pet a ``pet_matches`` gate admits, sorted.

    The display-side mirror of :func:`pet_matches`: same category semantics
    (None/empty -> the default "any pet" set, misc excluded), but it *names*
    the pets instead of testing one. Task surfaces use it to answer "which
    pets actually count?" — a question a bare category key can't."""
    if not categories:
        norms = ALL_PETS
    else:
        norms = frozenset().union(
            *(PET_CATEGORIES.get(c, frozenset()) for c in categories)
        ) if categories else frozenset()
    return tuple(sorted(PET_DISPLAY_BY_NORM[n] for n in norms
                        if n in PET_DISPLAY_BY_NORM))


def pet_category_of(pet_name: str) -> tuple[str, ...]:
    """Category keys a pet belongs to (a pet may sit in several — the raids
    pets are boss pets too). Empty when the name isn't catalogued."""
    n = _norm(pet_name)
    return tuple(k for k, names in PET_CATEGORIES.items() if n in names)


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


# ── typed-name resolution (manual submissions) ───────────────────────────────

def _loose(value) -> str:
    """``_norm`` plus punctuation folded away, so ``lil zik``, ``Lil' zik`` and
    ``lil'zik`` meet: apostrophes and dots vanish, hyphens become spaces."""
    text = _norm(value).replace("-", " ")
    text = "".join(ch for ch in text if ch.isalnum() or ch == " ")
    return " ".join(text.split())


def _source_words(source) -> set:
    # "pet" is in many pet names and says nothing about which one.
    return {w for w in _loose(source).split() if len(w) >= 3 and w != "pet"}


def resolve_pet_name(raw, source=None, extra_names=()):
    """``(match, candidates)`` for a pet name a member typed.

    ``match`` is the display spelling when the input points at exactly one
    pet, else None; ``candidates`` are the pets it might mean (empty when
    nothing comes close). Tried in order: an exact name, then names that
    contain what was typed ("Smol", "graardor"), then near spellings
    ("Olmet"). Several hits are narrowed by ``source`` (the boss, skill or
    activity), so "Smol" from "Sol Heredit" is Smol heredit, not Smolcano.
    ``extra_names`` adds pets this catalogue doesn't list yet (names the
    plugin has already recorded).
    """
    import difflib

    names = {}
    for name in PET_DISPLAY_BY_NORM.values():
        names.setdefault(_loose(name), name)
    for name in extra_names or ():
        if name and str(name).strip():
            names.setdefault(_loose(name), str(name).strip())
    key = _loose(raw)
    if not key:
        return None, ()
    if key in names:
        return names[key], (names[key],)

    hits = [k for k in names if key in k or k.replace(" ", "") == key.replace(" ", "")]
    fuzzy = False
    if not hits:
        hits = difflib.get_close_matches(key, list(names), n=5, cutoff=0.75)
        fuzzy = True
    if len(hits) > 1 and source:
        words = _source_words(source)
        skill = _loose(source)
        narrowed = [k for k in hits
                    if words & set(k.split())
                    or _loose(SKILLING_PET_SKILL.get(_norm(names[k])) or "") == skill]
        if narrowed:
            hits = narrowed
    candidates = tuple(sorted({names[k] for k in hits}, key=str.lower))
    if len(hits) == 1:
        # A near spelling is only taken on its own when it is very close;
        # anything looser is offered back as a suggestion.
        if not fuzzy or difflib.SequenceMatcher(None, key, hits[0]).ratio() >= 0.85:
            return names[hits[0]], candidates
        return None, candidates
    return None, candidates


def pet_name_choices(typed, extra_names=()):
    """Pet names for an autocomplete box: names containing what was typed,
    those starting with it first. Empty input lists every pet."""
    names = {}
    for name in list(PET_DISPLAY_BY_NORM.values()) + list(extra_names or ()):
        if name and str(name).strip():
            names.setdefault(_loose(name), str(name).strip())
    key = _loose(typed)
    hits = [k for k in names if key in k]
    hits.sort(key=lambda k: (not k.startswith(key), k))
    return [names[k] for k in hits]
