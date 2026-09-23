"""Slayer master registry and slayer assignment catalog.

Why this exists: the plugin reports a completed slayer task with the RAW value
of the ``SLAYER_MASTER`` varbit (4067). The game has no table of master display
names, so the name is resolved here, on the server, and so is the question that
actually matters — whether a completion counts toward an event goal ("10 tasks
from Duradel", "25 tasks, no Turael skipping"). The plugin never filters.

**Master ids.** All ten are confirmed from the game cache (2026-09-16). The
``slayer_master_task`` dbtable (114) holds one row per assignment a master can
give, with the master id in column 0, and each row's gameval name starts with
that master's name: 1 ``turael_rats``, 2 ``mazchna_crabs``, 3
``vannaka_crabs``, 4 ``chaeldar_wyrms``, 5 ``duradel_drakes``, 6
``nieve_drakes``, 7 ``krystillia_pirates``, 8 ``konar_brinerats``, 9
``spria_sourhogs``, 10 ``mortimer_banshees``. The assignment lists agree (1 and
9 hand out Turael's low-level list, only 8's rows carry areas), and 7 and 10
match RuneLite's own ``KRYSTILIA_SLAYER_MASTER``/``MORTIMER_SLAYER_MASTER``, so
the table's ids are the varbit's. Id 11 exists too (``leagues_cows``,
``leagues_birds``): a Leagues-only master, deliberately unnamed here, whose
completions arrive with the seasonal world type and never reach an event. Rows
store the raw id, so correcting this table would correct history.

**Alternates share a slot.** Aya replaces Turael after While Guthix Sleeps,
Achtryn replaces Mazchna, Kuradal replaces Duradel, Steve replaces Nieve. The
game treats each pair as one master — same assignments, same points — so they
share an id here and either name resolves to it.

**Reset masters.** Turael/Aya and Spria award no points and reset the task
streak; they are what "Turael skipping" uses to shed an unwanted task in
seconds. A ``slayer_target`` event task excludes them unless the organiser
says otherwise (:data:`DEFAULT_EXCLUDED_MASTER_IDS`), and so do a group's
Discord notifications unless its ``slayer_excluded_masters`` setting says
otherwise (:func:`excluded_master_ids_from_config`).

**Assignment names** mirror RuneLite's ``Task`` enum, which is keyed on the
game cache's own spelling (``DBTableID.SlayerTask`` ``COL_NAME_UPPERCASE``) and
matched case-insensitively — the plugin sends that cache name, so a
case-insensitive fold here is exact, never fuzzy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

__all__ = [
    "BOSS_TASK_ID",
    "DEFAULT_EXCLUDED_MASTER_IDS",
    "RESET_MASTER_IDS",
    "SLAYER_MASTERS",
    "SLAYER_TASK_NAMES",
    "SlayerMaster",
    "canonical_task_name",
    "catalog_records",
    "excluded_master_ids_from_config",
    "master_by_id",
    "master_by_name",
    "master_name",
    "normalize_master_ids",
    "normalize_task_names",
]

#: ``SLAYER_TARGET`` varp value meaning "a boss task"; the boss itself is then
#: in the ``SLAYER_TARGET_BOSSID`` varbit.
BOSS_TASK_ID = 98


@dataclass(frozen=True)
class SlayerMaster:
    id: int
    name: str
    aliases: tuple = ()
    #: False for the masters whose tasks earn no reward points.
    awards_points: bool = True
    #: True for the masters whose tasks reset the streak (Turael skipping).
    resets_streak: bool = False
    #: True once the id has been confirmed (game cache, RuneLite, live data).
    verified: bool = False

    @property
    def names(self) -> tuple:
        return (self.name,) + tuple(self.aliases)

    @property
    def label(self) -> str:
        """The name plus the alternates who stand in for this master, for a
        picker ("Turael / Aya"). An alias containing the name is a longer
        form of it ("Konar quo Maten"), not another NPC, so it is left out."""
        others = tuple(a for a in self.aliases if self.name.lower() not in a.lower())
        return " / ".join((self.name,) + others)


SLAYER_MASTERS: tuple = (
    SlayerMaster(1, "Turael", ("Aya",), awards_points=False, resets_streak=True, verified=True),
    SlayerMaster(2, "Mazchna", ("Achtryn",), verified=True),
    SlayerMaster(3, "Vannaka", verified=True),
    SlayerMaster(4, "Chaeldar", verified=True),
    SlayerMaster(5, "Duradel", ("Kuradal",), verified=True),
    SlayerMaster(6, "Nieve", ("Steve",), verified=True),
    SlayerMaster(7, "Krystilia", verified=True),
    SlayerMaster(8, "Konar", ("Konar quo Maten",), verified=True),
    SlayerMaster(9, "Spria", awards_points=False, resets_streak=True, verified=True),
    SlayerMaster(10, "Mortimer", verified=True),
)

_BY_ID = {m.id: m for m in SLAYER_MASTERS}
_BY_NAME = {n.lower(): m for m in SLAYER_MASTERS for n in m.names}

#: Masters whose tasks reset the streak and earn nothing.
RESET_MASTER_IDS = frozenset(m.id for m in SLAYER_MASTERS if m.resets_streak)
#: What a ``slayer_target`` task excludes when its author says nothing.
DEFAULT_EXCLUDED_MASTER_IDS = RESET_MASTER_IDS


def _as_int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
    except Exception:
        return None
    if text.isdigit():
        return int(text)
    return None


def master_by_id(master_id) -> Optional[SlayerMaster]:
    mid = _as_int(master_id)
    return _BY_ID.get(mid) if mid is not None else None


def master_name(master_id) -> Optional[str]:
    """Display name for a raw varbit value, or None when it is unknown."""
    master = master_by_id(master_id)
    return master.name if master else None


def master_by_name(name) -> Optional[SlayerMaster]:
    if name is None:
        return None
    return _BY_NAME.get(" ".join(str(name).strip().lower().split()))


def normalize_master_ids(raw: Iterable) -> list:
    """Sorted, deduplicated master ids from a mix of ids and names.

    Raises ``ValueError`` naming every entry it could not resolve — the
    validator turns that into a 422 that lists them all at once.
    """
    ids: set = set()
    unknown: list = []
    for entry in raw or ():
        master = master_by_id(entry) or master_by_name(entry)
        if master is None:
            unknown.append(str(entry).strip() or "(empty)")
            continue
        ids.add(master.id)
    if unknown:
        raise ValueError("Unknown slayer master(s): " + ", ".join(unknown))
    return sorted(ids)


def excluded_master_ids_from_config(raw) -> frozenset:
    """The masters a group's ``slayer_excluded_masters`` setting leaves out of
    its Discord notifications.

    ``None`` means the group never saved the setting, so the reset masters
    are left out, the same default an event task uses. An empty string is a
    saved choice to leave nobody out. Otherwise the value is the registry's
    stored form, comma-separated master ids ("1,9"). Anything that is not a
    known id is ignored rather than raised: this runs on the submission path,
    and a bad stored value must not cost a notification.
    """
    if raw is None:
        return DEFAULT_EXCLUDED_MASTER_IDS
    ids = set()
    for part in str(raw).split(","):
        master = master_by_id(part)
        if master is not None:
            ids.add(master.id)
    return frozenset(ids)


def catalog_records() -> list:
    """The registry as the task builder's picker consumes it."""
    return [
        {
            "id": m.id,
            "name": m.name,
            "aliases": list(m.aliases),
            "awards_points": m.awards_points,
            "resets_streak": m.resets_streak,
            "verified": m.verified,
        }
        for m in SLAYER_MASTERS
    ]


#: Every assignment the game can hand out, in cache spelling (RuneLite's
#: ``Task`` enum, 2026-09-10). Boss tasks appear under their own names.
SLAYER_TASK_NAMES: tuple = (
    "Aberrant spectres", "Abyssal demons", "The Abyssal Sire",
    "The Alchemical Hydra", "Ankou", "Aquanites", "Araxxor", "Araxytes",
    "Aviansies", "Bandits", "Banshees", "Barrows Brothers", "Basilisks", "Bats",
    "Bears", "Birds", "Black demons", "Black dragons", "Black Knights",
    "Bloodveld", "Blue dragons", "Brine rats", "Callisto", "Catablepon",
    "Cave bugs", "Cave crawlers", "Cave horrors", "Cave kraken", "Cave slimes",
    "Cerberus", "Chaos druids", "The Chaos Elemental", "The Chaos Fanatic",
    "Cockatrice", "Cows", "Crabs", "Crawling hands", "Crazy Archaeologists",
    "Crocodiles", "Custodian Stalkers", "Dagannoth", "Dagannoth Kings",
    "Dark beasts", "Dark warriors", "Deranged Archaeologist", "Dogs", "Drakes",
    "Duke Sucellus", "Dust devils", "Dwarves", "Earth warriors", "Elves", "Ents",
    "Fever spiders", "Fire giants", "Fleshcrawlers", "Fossil island wyverns",
    "Frost dragons", "Gargoyles", "General Graardor", "Ghosts", "Ghouls",
    "The Giant Mole", "Goblins", "Greater demons", "Green dragons",
    "The Grotesque Guardians", "Gryphons", "Harpie bug swarms", "Hellhounds",
    "Hill giants", "Hobgoblins", "Hydras", "Icefiends", "Ice giants",
    "Ice warriors", "Infernal mages", "TzTok-Jad", "Jellies", "Jungle horrors",
    "Kalphites", "The Kalphite Queen", "Killerwatts", "The King Black Dragon",
    "The Cave Kraken Boss", "Kree'arra", "K'ril Tsutsaroth", "Kurask",
    "Lava Dragons", "Lesser demons", "Lesser Nagua", "Lizardmen", "Lizards",
    "The Maggot King", "Magic axes", "Mammoths", "Metal dragons", "Minotaurs",
    "Mogres", "Molanisks", "Monkeys", "Moss giants", "Mutated zygomites",
    "Nechryael", "Ogres", "Otherworldly beings", "The Phantom Muspah", "Pirates",
    "Pyrefiends", "Rats", "Red dragons", "Revenants", "Rockslugs", "Rogues",
    "Sarachnis", "Scabarites", "Scorpia", "Scorpions", "Sea snakes", "Shades",
    "Shadow warriors", "The Shellbane Gryphon", "Skeletal wyverns", "Skeletons",
    "Smoke devils", "Sourhogs", "Spiders", "Spiritual creatures", "Suqahs",
    "Terror dogs", "The Leviathan", "The Whisperer",
    "The Thermonuclear Smoke Devil", "Trolls", "Turoth", "Tzhaar", "Vampyres",
    "Vardorvis", "Venators", "Venenatis", "Vet'ion", "Vorkath", "Wall beasts",
    "Warped Creatures", "Waterfiends", "Werewolves", "Wolves", "Wyrms",
    "Commander Zilyana", "Zombies", "TzKal-Zuk", "Zulrah",
)


def _norm(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


_TASK_BY_NORM = {_norm(n): n for n in SLAYER_TASK_NAMES}


def canonical_task_name(name) -> Optional[str]:
    """Catalog spelling of an assignment name, or None when it is not one.

    Exact-or-normalized only, never fuzzy: "Dagannoth" and "Dagannoth Kings"
    are two assignments that differ by one word.
    """
    return _TASK_BY_NORM.get(_norm(name))


def normalize_task_names(raw: Iterable) -> list:
    """Catalog spellings, deduplicated, input order kept.

    Raises ``ValueError`` naming every entry that is not an assignment.
    """
    out: list = []
    unknown: list = []
    for entry in raw or ():
        canonical = canonical_task_name(entry)
        if canonical is None:
            unknown.append(str(entry).strip() or "(empty)")
        elif canonical not in out:
            out.append(canonical)
    if unknown:
        raise ValueError("Unknown slayer task(s): " + ", ".join(unknown))
    return out
