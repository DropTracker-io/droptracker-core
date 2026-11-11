"""Typed item and NPC names on Discord manual submissions.

``/submit`` autocompletes but Discord does not force a pick, and the Submit
button's modal cannot autocomplete at all, so both surfaces hand us whatever
the member typed. The intake looks names up by exact spelling, which turned
every typo ("Zulra"), dropped apostrophe ("Tumekens shadow") and abbreviation
("tbow", "cox") into a rejection.

:func:`resolve_name` places a typed name in the catalog before the submission
is sent: a name that points at one entry is corrected, a name that could be a
few is offered back as "did you mean?" buttons, and a name nothing comes close
to goes through untouched so the intake can still accept something new.

Everything here is pure (no db, Discord or Redis imports): the catalog rows
and the Redis connection are passed in, so it stays unit-testable. Pets have
their own resolver (``utils/osrs_pets.resolve_pet_name``).
"""
from __future__ import annotations

import difflib
import json
import time
from typing import Callable, Iterable, Optional

#: "Did you mean?" buttons: ``dtsubmit:namepick:<token>:<choice>`` where
#: ``<choice>`` is a candidate index, ``t`` (keep what I typed) or ``x`` (cancel).
NAME_PICK_PREFIX = "dtsubmit:namepick:"
NAME_PICK_MAX = 5
PICK_AS_TYPED = "t"
PICK_CANCEL = "x"

#: Capped Redis list of every typed name that needed help, newest first. This
#: is where new aliases come from (and the only record of how often it happens).
RESOLUTION_LOG_KEY = "manual:name_resolutions"
RESOLUTION_LOG_MAX = 5000

# A near spelling is corrected on its own only when it is very close AND
# clearly ahead of the runner-up; anything looser is asked about.
_FUZZY_CUTOFF = 0.72
_FUZZY_AUTO = 0.88
_FUZZY_AUTO_GAP = 0.08
_FUZZY_UNTRACKED_CUTOFF = 0.8
_MIN_CONTAINS_LEN = 3

# Abbreviations a spelling match can never find. A target that is not in the
# catalog is ignored, so a stale entry is harmless. Grow these from
# RESOLUTION_LOG_KEY ("asked"/"unknown" rows show what members really type).
ITEM_ALIASES = {
    "tbow": "Twisted bow",
    "dwh": "Dragon warhammer",
    "bcp": "Bandos chestplate",
    "tassets": "Bandos tassets",
    "dhcb": "Dragon hunter crossbow",
    "dhl": "Dragon hunter lance",
    "acb": "Armadyl crossbow",
    "claws": "Dragon claws",
    "d claws": "Dragon claws",
    "dclaws": "Dragon claws",
    "whip": "Abyssal whip",
    "d pick": "Dragon pickaxe",
    "dpick": "Dragon pickaxe",
    "d axe": "Dragon axe",
    "kodai": "Kodai insignia",
    "rapier": "Ghrazi rapier",
    "sang": "Sanguinesti staff (uncharged)",
    "sang staff": "Sanguinesti staff (uncharged)",
    "scythe": "Scythe of vitur (uncharged)",
    "shadow": "Tumeken's shadow (uncharged)",
    "fang": "Osmumten's fang",
    "avernic": "Avernic defender hilt",
    "enh": "Enhanced crystal weapon seed",
    "enhanced": "Enhanced crystal weapon seed",
    "enhanced seed": "Enhanced crystal weapon seed",
    "armour seed": "Crystal armour seed",
    "armor seed": "Crystal armour seed",
    "ely": "Elysian sigil",
    "dex": "Dexterous prayer scroll",
    "dex scroll": "Dexterous prayer scroll",
    "arcane scroll": "Arcane prayer scroll",
    "blowpipe": "Toxic blowpipe",
    "bp": "Toxic blowpipe",
    "voidwaker": "Voidwaker hilt",
    "quiver": "Dizana's quiver (uncharged)",
    "tonalztics": "Tonalztics of ralos (uncharged)",
    "ralos": "Tonalztics of ralos (uncharged)",
    "bludgeon": "Abyssal bludgeon",
    "zenyte": "Zenyte shard",
    "visage": "Draconic visage",
    "ward": "Elidinis' ward",
}

NPC_NAME_ALIASES = {
    "cox": "Chambers of Xeric",
    "raids": "Chambers of Xeric",
    "raids 1": "Chambers of Xeric",
    "cox cm": "Chambers of Xeric Challenge Mode",
    "cm": "Chambers of Xeric Challenge Mode",
    "cms": "Chambers of Xeric Challenge Mode",
    "tob": "Theatre of Blood",
    "raids 2": "Theatre of Blood",
    "hmt": "Theatre of Blood: Hard Mode",
    "tob hm": "Theatre of Blood: Hard Mode",
    "toa": "Tombs of Amascut",
    "raids 3": "Tombs of Amascut",
    "toa expert": "Tombs of Amascut: Expert Mode",
    "corp": "Corporeal Beast",
    "cg": "The Corrupted Gauntlet",
    "kq": "Kalphite Queen",
    "kbd": "King Black Dragon",
    "bandos": "General Graardor",
    "graardor": "General Graardor",
    "sara": "Commander Zilyana",
    "saradomin": "Commander Zilyana",
    "zilyana": "Commander Zilyana",
    "arma": "Kree'arra",
    "armadyl": "Kree'arra",
    "kree": "Kree'arra",
    "zammy": "K'ril Tsutsaroth",
    "zamorak": "K'ril Tsutsaroth",
    "kril": "K'ril Tsutsaroth",
    "hydra": "Alchemical Hydra",
    "cerb": "Cerberus",
    "sire": "Abyssal Sire",
    "thermy": "Thermonuclear smoke devil",
    "muspah": "Phantom Muspah",
    "pnm": "Phosani's Nightmare",
    "phosani": "Phosani's Nightmare",
    "phosanis": "Phosani's Nightmare",
    "nm": "The Nightmare",
    "nightmare": "The Nightmare",
    "jad": "TzTok-Jad",
    "zuk": "TzKal-Zuk",
    "inferno": "TzKal-Zuk",
    "colosseum": "Fortis Colosseum",
    "colo": "Fortis Colosseum",
    "sol": "Fortis Colosseum",
    "levi": "The Leviathan",
    "leviathan": "The Leviathan",
    "whisperer": "The Whisperer",
    "vard": "Vardorvis",
    "duke": "Duke Sucellus",
    "gg": "Grotesque Guardians",
    "ggs": "Grotesque Guardians",
    "moons": "Lunar Chest",
    "moons of peril": "Lunar Chest",
    "titans": "Royal Titans",
    "doom": "Doom of Mokhaiotl",
    "huey": "The Hueycoatl",
    "hueycoatl": "The Hueycoatl",
    "mole": "Giant Mole",
    "chaos ele": "Chaos Elemental",
    "vetion": "Vet'ion",
    "calvarion": "Calvar'ion",
    "mimic": "The Mimic",
    "raids 4": "Fractured Archive",
}


def fold(value) -> str:
    """The key two spellings of one name share: case, apostrophes, dots and
    brackets vanish; hyphens and underscores become spaces."""
    text = str(value or "").lower().replace("-", " ").replace("_", " ")
    text = "".join(ch for ch in text if ch.isalnum() or ch == " ")
    return " ".join(text.split())


class NameCatalog:
    """The names a typed value is matched against.

    ``rows`` are ``(name, tracked)`` pairs. ``tracked`` means the entry has
    real submissions behind it: the catalog also holds cosmetic variants and
    stray rows ("Scythe", "Duke"), and those must not win over the real thing.
    ``key_fn`` optionally maps a name to a looser identity (NPCs:
    ``utils.npc_names.npc_match_key``) for names spelled outright differently.
    """

    def __init__(self, rows: Iterable, aliases: Optional[dict] = None,
                 key_fn: Optional[Callable] = None):
        self.names: dict = {}      # fold -> display name (tracked spelling wins)
        self.tracked: set = set()  # folds with real submissions behind them
        self.by_key: dict = {}     # key_fn(name) -> fold (tracked wins)
        self.key_fn = key_fn
        for name, tracked in rows:
            name = str(name or "").strip()
            key = fold(name)
            if not key:
                continue
            if key not in self.names or (tracked and key not in self.tracked):
                self.names[key] = name
            if tracked:
                self.tracked.add(key)
        if key_fn is not None:
            for key in self.names:
                try:
                    loose = key_fn(self.names[key])
                except Exception:
                    continue
                if not loose:
                    continue
                current = self.by_key.get(loose)
                if current is None or (key in self.tracked and current not in self.tracked):
                    self.by_key[loose] = key
        self.tracked_list = sorted(self.tracked)
        self.aliases = {}
        for short, target in (aliases or {}).items():
            target_key = fold(target)
            if target_key in self.names:
                self.aliases[fold(short)] = target_key

    def __bool__(self) -> bool:
        return bool(self.names)

    def _initials(self) -> dict:
        """Initialism -> tracked folds ("cox" -> chambers of xeric), built once."""
        cached = getattr(self, "_initials_cache", None)
        if cached is not None:
            return cached
        table: dict = {}
        for key in self.tracked_list:
            words = key.split()
            if len(words) < 2:
                continue
            forms = {"".join(w[0] for w in words)}
            if words[0] == "the" and len(words) > 2:
                forms.add("".join(w[0] for w in words[1:]))
            for form in forms:
                table.setdefault(form, []).append(key)
        self._initials_cache = table
        return table


def resolve_name(raw, catalog: Optional[NameCatalog]) -> tuple:
    """``(match, candidates)`` for a typed item or NPC name.

    ``match`` is the catalog spelling when the input points at one entry, else
    None. ``candidates`` are the entries it might mean, best first (at most
    NAME_PICK_MAX); empty when nothing comes close. Tried in order: a known
    abbreviation, the exact name (ignoring case and punctuation), a looser
    identity (``key_fn``), names that contain what was typed, initials, then
    near spellings. Only entries with real submissions are guessed at; the
    rest of the catalog is a last resort and is always asked about.
    """
    key = fold(raw)
    if not key or not catalog:
        return None, ()
    names = catalog.names

    def shown(folds) -> tuple:
        out: list = []
        for f in folds:
            if names[f] not in out:
                out.append(names[f])
        return tuple(out[:NAME_PICK_MAX])

    alias = catalog.aliases.get(key)
    if alias:
        return names[alias], (names[alias],)
    exact = key if key in names else None
    if exact and exact in catalog.tracked:
        return names[exact], (names[exact],)
    if catalog.key_fn is not None:
        try:
            loose = catalog.by_key.get(catalog.key_fn(str(raw)))
        except Exception:
            loose = None
        if loose and (loose in catalog.tracked or not exact):
            return names[loose], (names[loose],)

    contained: list = []
    if len(key) >= _MIN_CONTAINS_LEN:
        contained = [f for f in catalog.tracked_list
                     if f != key and (f.startswith(key) or f" {key}" in f)]
        contained.sort(key=lambda f: (not f.startswith(key), len(f), f))
    if exact:
        # In the catalog, but nobody has ever received it. Taken as typed
        # unless something real has the same words in it ("Scythe" the stray
        # row vs. "Scythe of vitur").
        if not contained:
            return names[exact], (names[exact],)
        return None, shown([exact] + contained)
    if len(contained) == 1 and contained[0].startswith(key):
        return names[contained[0]], shown(contained)
    if contained:
        return None, shown(contained)

    if " " not in key and 2 <= len(key) <= 5:
        initials = catalog._initials().get(key)
        if initials:
            return None, shown(sorted(initials, key=lambda f: (len(f), f)))

    close = difflib.get_close_matches(key, catalog.tracked_list, n=NAME_PICK_MAX,
                                      cutoff=_FUZZY_CUTOFF)
    if close:
        ratios = [difflib.SequenceMatcher(None, key, f).ratio() for f in close]
        clear = len(close) == 1 or ratios[0] - ratios[1] >= _FUZZY_AUTO_GAP
        if ratios[0] >= _FUZZY_AUTO and clear:
            return names[close[0]], shown(close)
        return None, shown(close)
    close = difflib.get_close_matches(key, list(names), n=NAME_PICK_MAX,
                                      cutoff=_FUZZY_UNTRACKED_CUTOFF)
    return None, shown(close)


def suggest_names(raw, catalog: Optional[NameCatalog]) -> list:
    """Names for an autocomplete box when a plain search found too little:
    the entry the input resolves to, or the ones it might mean."""
    match, candidates = resolve_name(raw, catalog)
    if match:
        return [match]
    return list(candidates)


# ── which fields of a submission are typed names ─────────────────────────────

#: ``build_manual_payload`` keyword -> (catalog kind, what to call it, with its article).
_FIELD_KINDS = {
    "drop": {"item_name": ("item", "an item"), "npc_name": ("npc", "a boss or NPC")},
    "clog": {"item_name": ("item", "an item"), "npc_name": ("npc", "a boss or NPC")},
    "pb": {"npc_name": ("npc", "a boss")},
    # The pet itself goes through the pet resolver.
    "pet": {"npc_name": ("npc", "a boss or NPC")},
}


def resolve_fields(sub_type: str, fields: dict, *, items: Optional[NameCatalog],
                   npcs: Optional[NameCatalog], confirmed=()) -> tuple:
    """``(fields, corrections, question)`` for one submission's typed names.

    ``fields`` comes back with every name that resolved replaced by its
    catalog spelling. ``corrections`` lists ``(field, typed, resolved)`` for
    the ones that changed by more than case. ``question`` is None, or a dict
    (``field``, ``kind``, ``label``, ``raw``, ``candidates``) for the first
    name that needs the member to choose; nothing should be sent until it is
    answered. Fields in ``confirmed`` were already answered and are left alone.
    """
    out = dict(fields)
    corrections: list = []
    catalogs = {"item": items, "npc": npcs}
    for field, (kind, label) in _FIELD_KINDS.get(sub_type, {}).items():
        raw = (out.get(field) or "").strip() if isinstance(out.get(field), str) else ""
        if not raw or field in confirmed:
            continue
        match, candidates = resolve_name(raw, catalogs[kind])
        if match:
            out[field] = match
            if fold(match) != fold(raw):
                corrections.append((field, raw, match))
            continue
        if candidates:
            return out, corrections, {
                "field": field, "kind": kind, "label": label,
                "raw": raw, "candidates": tuple(candidates),
            }
    return out, corrections, None


def question_text(question: dict) -> str:
    """The member-facing "did you mean?" sentence for a :func:`resolve_fields`
    question."""
    raw, label = question["raw"], question["label"]
    candidates = question["candidates"]
    if len(candidates) == 1:
        return (f"We don't have {label} called “{raw}”. Did you mean "
                f"**{candidates[0]}**? Nothing has been submitted yet.")
    return (f"We don't have {label} called “{raw}”. Pick the one you meant "
            "below. Nothing has been submitted yet.")


def correction_lines(corrections) -> list:
    """Lines telling the member what a corrected name was recorded as."""
    return [f"-# Recorded as **{resolved}** (you typed “{typed}”)."
            for _field, typed, resolved in corrections]


def pick_components(token: str, candidates, *, allow_as_typed: bool = True) -> list:
    """Raw Discord components for a "did you mean?" question: one button per
    candidate, then "keep what I typed" and Cancel."""
    picks = [
        {"type": 2, "style": 1 if len(candidates) == 1 else 2, "label": str(name)[:80],
         "custom_id": f"{NAME_PICK_PREFIX}{token}:{i}"}
        for i, name in enumerate(list(candidates)[:NAME_PICK_MAX])
    ]
    extras = []
    if allow_as_typed:
        extras.append({"type": 2, "style": 2, "label": "Keep what I typed",
                       "custom_id": f"{NAME_PICK_PREFIX}{token}:{PICK_AS_TYPED}"})
    extras.append({"type": 2, "style": 4, "label": "Cancel",
                   "custom_id": f"{NAME_PICK_PREFIX}{token}:{PICK_CANCEL}"})
    rows = []
    if picks:
        rows.append({"type": 1, "components": picks})
    rows.append({"type": 1, "components": extras})
    return rows


def parse_pick(custom_id) -> Optional[tuple]:
    """``(token, choice)`` from a name-pick button id, else None. ``choice`` is
    an int candidate index, PICK_AS_TYPED or PICK_CANCEL."""
    if not isinstance(custom_id, str) or not custom_id.startswith(NAME_PICK_PREFIX):
        return None
    token, _, choice = custom_id[len(NAME_PICK_PREFIX):].rpartition(":")
    if not token:
        return None
    if choice in (PICK_AS_TYPED, PICK_CANCEL):
        return token, choice
    if choice.isdigit():
        return token, int(choice)
    return None


def log_resolution(conn, *, surface: str, kind: str, raw: str, outcome: str,
                   chosen: Optional[str] = None, candidates=()) -> None:
    """Record one typed name that needed help. Best-effort: never raises.

    ``outcome``: ``auto`` (corrected without asking), ``asked`` (buttons
    shown), ``picked`` / ``as_typed`` / ``cancelled`` (the answer).
    """
    if conn is None:
        return
    try:
        entry = json.dumps({
            "ts": int(time.time()), "surface": surface, "kind": kind,
            "raw": str(raw)[:100], "outcome": outcome, "chosen": chosen,
            "candidates": list(candidates)[:NAME_PICK_MAX],
        })
        conn.lpush(RESOLUTION_LOG_KEY, entry)
        conn.ltrim(RESOLUTION_LOG_KEY, 0, RESOLUTION_LOG_MAX - 1)
    except Exception:
        pass
