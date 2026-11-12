"""Event task generator — the "Fill for me" core (docs/TASK_GENERATOR_PLAN.md).

Builds a balanced, sized set of event tasks from what DropTracker already
knows instead of asking an admin to hand-pick 25 tasks from a flat library:

- **Kill rates** — WOM's EHB kills/hour, falling back to our own
  ``npc_ehb_rates`` and, last, the rough figure on the encounter below.
- **Drop rates** — the wiki drop tables (``xenforo.dt_npc_loot``) for each
  boss's curated Clan Log uniques, overridden here where the wiki number is
  conditional (raids roll a unique first, then pick the item).
- **What the clan actually does** — distinct members with drops at each boss
  in the last 90 days (the NPC hourly rollup), for the "familiar"/"fresh"
  weighting.

Every candidate carries an estimated **team-hours** figure (expected hours of
one player's efficient play, split however the team likes). Difficulty is
that figure measured against the team's capacity for the event —
``team_size × days × hours_per_day`` — so a 5-person weekend and a 40-person
fortnight get different "hard" tasks from the same catalog.

This module is PURE: no DB, Redis or network (tests load it directly). The
DB-backed catalog assembly lives in ``web_api/task_generator_catalog.py``;
the routes in ``web_api/routes/event_task_generator.py``.
"""
from __future__ import annotations

import json
import math
import random
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

#: EVENT_TASK_DIFFICULTIES, easiest first. The UI labels them Easy / Medium /
#: Hard / Elite (web lib/events.ts TASK_DIFFICULTY_LABELS); the rune names are
#: the stored keys because board-game tiles already roll from these pools.
DIFFICULTIES = ("air", "water", "earth", "fire")
DIFFICULTY_LABELS = {"air": "Easy", "water": "Medium", "earth": "Hard", "fire": "Elite"}

#: Default points per tier. Admins can edit any task afterwards.
DEFAULT_POINTS = {"air": 1, "water": 2, "earth": 3, "fire": 5}

#: Upper edge of each tier as a share of the team's capacity, with an absolute
#: floor in hours so tiny events don't ask for "3 Zulrah kills" as Medium.
#: A candidate above the Elite edge is left out entirely.
TIER_SHARE = {"air": 0.005, "water": 0.02, "earth": 0.05, "fire": 0.15}
TIER_FLOOR_HOURS = {"air": 0.25, "water": 0.75, "earth": 2.0, "fire": 5.0}

#: How much the average member plays event content per day.
ACTIVITY_HOURS_PER_DAY = {"casual": 0.75, "normal": 1.5, "hardcore": 3.0}

CONTENT_CATEGORIES = {
    "raids": "Raids",
    "gwd": "God Wars Dungeon",
    "dt2": "Desert Treasure II bosses",
    "bosses": "Other bosses",
    "slayer": "Slayer bosses & tasks",
    "wilderness": "Wilderness",
    "group": "Barrows, Dagannoth Kings & Moons",
    "skilling": "Skilling",
    "general": "Anywhere (loot value)",
}

TASK_KINDS = {
    "uniques": "Boss uniques",
    "kc": "Kill counts",
    "pets": "Pets",
    "ca": "Combat achievements",
    "xp": "Skill XP",
    "slayer": "Slayer tasks",
    "loot": "Loot value",
}

#: Relative share of picks per kind. Normalized per kind at pick time, so 21
#: skills don't out-vote 45 bosses just by being many candidates. Uniques are
#: what makes an event exciting, so they come up most; KC keeps every board
#: completable.
KIND_WEIGHT = {"uniques": 3.0, "kc": 2.5, "pets": 0.6, "ca": 0.8,
               "xp": 1.0, "slayer": 0.4, "loot": 0.4}

CLAN_FOCUS_MODES = ("off", "familiar", "fresh")

#: How many tasks one boss / skill may contribute to a single fill.
MAX_PER_FAMILY = {"encounter": 2, "skill": 1, "slayer": 1, "loot": 1}

MAX_GENERATE = 100

# --------------------------------------------------------------------------- #
# Curated encounters
# --------------------------------------------------------------------------- #
# ``kc_npcs`` are npc_list names exactly as drops and kills arrive (first one
# is the canonical target). Uniques (attributable) and the pet (not
# attributable) come from the Clan Log sections whose ``npc_keys`` share an
# ``npc_match_key`` with ``kc_npcs`` — matched by key, not slug, so a catalog
# reseed that renames sections can't silently empty the generator (the v2
# catalog of 2026-10-19 did exactly that to a slug list).
# ``clog_keys`` adds npc_keys the boss's own names don't produce ("gauntlet"
# for the Corrupted Gauntlet); ``sections`` names sections with no npc_keys
# (shared sets like Virtus, the gorilla drops) by slug.
# ``short`` (optional) replaces ``label`` inside task labels.
# Overrides, all per kill / per completion for ONE player:
#   unique_rate — chance of any unique. Required where the wiki table is
#                 conditional (raids) or scattered across kills (DKs).
#   conditional — the wiki's per-item rates are conditional, so no
#                 "get this exact item" tasks (the any-unique task stays).
#   pet_rate    — pet chance when the wiki table has no pet row.
#   kph         — fallback kills/hour when neither WOM nor our derived table
#                 prices the boss.
# Rates are deliberately approximate: they size targets, they don't score.

_VIRTUS = ["desert_treasure_virtus_set"]

ENCOUNTERS: tuple[dict, ...] = (
    # Raids
    {"key": "chambers_of_xeric", "label": "Chambers of Xeric", "category": "raids",
     "kc_npcs": ["Chambers of Xeric", "Chambers of Xeric Challenge Mode"],
     "wom_metric": "chambers_of_xeric",
     "ca_monsters": ["Chambers of Xeric", "Chambers of Xeric: CM"],
     "unique_rate": 1 / 29, "conditional": True, "pet_rate": 1 / 1500, "unit": "completions"},
    {"key": "theatre_of_blood", "label": "Theatre of Blood", "category": "raids",
     "kc_npcs": ["Theatre of Blood", "Theatre of Blood: Hard Mode"],
     "wom_metric": "theatre_of_blood",
     "ca_monsters": ["Theatre of Blood", "Theatre of Blood: Hard Mode"],
     "unique_rate": 1 / 36, "conditional": True, "pet_rate": 1 / 650, "unit": "completions"},
    {"key": "tombs_of_amascut", "label": "Tombs of Amascut", "category": "raids",
     "kc_npcs": ["Tombs of Amascut", "Tombs of Amascut: Expert Mode"],
     "wom_metric": "tombs_of_amascut",
     "ca_monsters": ["Tombs of Amascut", "Tombs of Amascut: Expert Mode"],
     "unique_rate": 1 / 24, "conditional": True, "pet_rate": 1 / 1000, "unit": "completions"},
    # God Wars Dungeon
    {"key": "kree_arra", "label": "Kree'arra", "category": "gwd", "kc_npcs": ["Kree'arra"],
     "wom_metric": "kreearra", "pet_rate": 1 / 5000},
    {"key": "general_graardor", "label": "General Graardor", "category": "gwd",
     "kc_npcs": ["General Graardor"], "wom_metric": "general_graardor", "pet_rate": 1 / 5000},
    {"key": "commander_zilyana", "label": "Commander Zilyana", "category": "gwd",
     "kc_npcs": ["Commander Zilyana"], "wom_metric": "commander_zilyana", "pet_rate": 1 / 5000},
    {"key": "kril_tsutsaroth", "label": "K'ril Tsutsaroth", "category": "gwd",
     "kc_npcs": ["K'ril Tsutsaroth"], "wom_metric": "kril_tsutsaroth", "pet_rate": 1 / 5000},
    {"key": "nex", "label": "Nex", "category": "gwd", "kc_npcs": ["Nex"], "wom_metric": "nex",
     "unique_rate": 1 / 90},
    # Desert Treasure II
    {"key": "vardorvis", "label": "Vardorvis", "category": "dt2", "kc_npcs": ["Vardorvis"],
     "sections": _VIRTUS, "wom_metric": "vardorvis"},
    {"key": "duke_sucellus", "label": "Duke Sucellus", "category": "dt2",
     "kc_npcs": ["Duke Sucellus"], "sections": _VIRTUS, "wom_metric": "duke_sucellus"},
    {"key": "the_whisperer", "label": "The Whisperer", "category": "dt2",
     "kc_npcs": ["The Whisperer"], "sections": _VIRTUS, "wom_metric": "the_whisperer"},
    {"key": "the_leviathan", "label": "The Leviathan", "category": "dt2",
     "kc_npcs": ["The Leviathan"], "sections": _VIRTUS, "wom_metric": "the_leviathan"},
    # Other bosses
    {"key": "zulrah", "label": "Zulrah", "category": "bosses", "kc_npcs": ["Zulrah"],
     "wom_metric": "zulrah", "pet_rate": 1 / 4000},
    {"key": "vorkath", "label": "Vorkath", "category": "bosses", "kc_npcs": ["Vorkath"],
     "wom_metric": "vorkath"},
    {"key": "phantom_muspah", "label": "Phantom Muspah", "category": "bosses",
     "kc_npcs": ["Phantom Muspah"], "wom_metric": "phantom_muspah"},
    {"key": "kalphite_queen", "label": "Kalphite Queen", "category": "bosses",
     "kc_npcs": ["Kalphite Queen"], "wom_metric": "kalphite_queen", "pet_rate": 1 / 3000},
    {"key": "hueycoatl", "label": "The Hueycoatl", "category": "bosses",
     "kc_npcs": ["The Hueycoatl"], "wom_metric": "the_hueycoatl"},
    {"key": "nightmare", "label": "The Nightmare", "category": "bosses",
     "kc_npcs": ["The Nightmare", "Phosani's Nightmare"],
     "wom_metric": "nightmare", "ca_monsters": ["The Nightmare", "Phosani's Nightmare"],
     "pet_rate": 1 / 1400},
    {"key": "amoxliatl", "label": "Amoxliatl", "category": "bosses", "kc_npcs": ["Amoxliatl"],
     "wom_metric": "amoxliatl"},
    {"key": "the_gauntlet", "label": "The Gauntlet", "category": "bosses",
     "kc_npcs": ["The Gauntlet"], "wom_metric": "the_gauntlet",
     "ca_monsters": ["Crystalline Hunllef"], "pet_rate": 1 / 2000, "unit": "completions"},
    {"key": "corrupted_gauntlet", "label": "The Corrupted Gauntlet", "category": "bosses",
     "kc_npcs": ["The Corrupted Gauntlet"], "clog_keys": ["gauntlet"],
     "wom_metric": "the_corrupted_gauntlet", "ca_monsters": ["Corrupted Hunllef"],
     "unique_rate": 1 / 24, "pet_rate": 1 / 800, "unit": "completions"},
    {"key": "scurrius", "label": "Scurrius", "category": "bosses", "kc_npcs": ["Scurrius"],
     "wom_metric": "scurrius"},
    {"key": "fortis_colosseum", "label": "Fortis Colosseum", "category": "bosses",
     "kc_npcs": ["Fortis Colosseum"], "wom_metric": "sol_heredit", "unit": "completions"},
    {"key": "tormented_demon", "label": "Tormented Demons", "short": "Tormented Demon",
     "category": "bosses", "kc_npcs": ["Tormented Demon"], "clog_keys": ["tormented-demons"],
     "kph": 45.0},
    {"key": "corporeal_beast", "label": "Corporeal Beast", "category": "bosses",
     "kc_npcs": ["Corporeal Beast"], "wom_metric": "corporeal_beast",
     "uniques": ["Spectral sigil", "Arcane sigil", "Elysian sigil", "Holy elixir"],
     "pet": "Pet dark core", "pet_rate": 1 / 5000},
    {"key": "yama", "label": "Yama", "category": "bosses", "kc_npcs": ["Yama"],
     "wom_metric": "yama"},
    {"key": "royal_titans", "label": "The Royal Titans", "category": "bosses",
     "kc_npcs": ["Royal Titans"], "wom_metric": "the_royal_titans",
     "ca_monsters": ["Royal Titans"]},
    {"key": "doom_of_mokhaiotl", "label": "Doom of Mokhaiotl", "category": "bosses",
     "kc_npcs": ["Doom of Mokhaiotl"], "wom_metric": "doom_of_mokhaiotl"},
    {"key": "giant_mole", "label": "Giant Mole", "category": "bosses",
     "kc_npcs": ["Giant Mole"], "wom_metric": "giant_mole"},
    {"key": "obor", "label": "Obor", "category": "bosses", "kc_npcs": ["Obor"],
     "wom_metric": "obor"},
    {"key": "bryophyta", "label": "Bryophyta", "category": "bosses", "kc_npcs": ["Bryophyta"],
     "wom_metric": "bryophyta"},
    {"key": "deranged_archaeologist", "label": "Deranged Archaeologist", "category": "bosses",
     "kc_npcs": ["Deranged archaeologist"], "wom_metric": "deranged_archaeologist"},
    {"key": "brutus", "label": "Brutus", "category": "bosses", "kc_npcs": ["Brutus"],
     "wom_metric": "brutus"},
    {"key": "maggot_king", "label": "Maggot King", "category": "bosses",
     "kc_npcs": ["Maggot King"], "wom_metric": "maggot_king"},
    {"key": "mad_angel", "label": "The Mad Angel", "category": "bosses",
     "kc_npcs": ["Mad Angel"], "wom_metric": "mad_angel"},
    # Slayer bosses
    {"key": "sarachnis", "label": "Sarachnis", "category": "slayer", "kc_npcs": ["Sarachnis"],
     "wom_metric": "sarachnis"},
    {"key": "grotesque_guardians", "label": "Grotesque Guardians", "category": "slayer",
     "kc_npcs": ["Grotesque Guardians"],
     "wom_metric": "grotesque_guardians", "unique_rate": 1 / 75, "conditional": True,
     "pet_rate": 1 / 3000},
    {"key": "abyssal_sire", "label": "Abyssal Sire", "category": "slayer",
     "kc_npcs": ["Abyssal Sire"], "wom_metric": "abyssal_sire", "pet_rate": 1 / 2560},
    {"key": "kraken", "label": "Kraken", "category": "slayer", "kc_npcs": ["Kraken"],
     "wom_metric": "kraken", "unique_rate": 1 / 220, "pet_rate": 1 / 3000},
    {"key": "cerberus", "label": "Cerberus", "category": "slayer", "kc_npcs": ["Cerberus"],
     "wom_metric": "cerberus"},
    {"key": "araxxor", "label": "Araxxor", "category": "slayer", "kc_npcs": ["Araxxor"],
     "wom_metric": "araxxor"},
    {"key": "thermonuclear_smoke_devil", "label": "Thermonuclear Smoke Devil",
     "category": "slayer", "kc_npcs": ["Thermonuclear smoke devil"],
     "wom_metric": "thermonuclear_smoke_devil",
     "ca_monsters": ["Thermonuclear Smoke Devil"], "pet_rate": 1 / 3000},
    {"key": "alchemical_hydra", "label": "Alchemical Hydra", "category": "slayer",
     "kc_npcs": ["Alchemical Hydra"], "wom_metric": "alchemical_hydra", "pet_rate": 1 / 3000},
    {"key": "demonic_gorillas", "label": "Demonic Gorillas", "category": "slayer",
     "kc_npcs": ["Demonic gorilla"], "sections": ["slayer_demonic_gorillas"],
     "ca_monsters": ["Demonic Gorilla"]},
    {"key": "skotizo", "label": "Skotizo", "category": "slayer", "kc_npcs": ["Skotizo"],
     "wom_metric": "skotizo"},
    {"key": "shellbane", "label": "Shellbane Gryphon", "category": "slayer",
     "kc_npcs": ["Shellbane gryphon"], "wom_metric": "shellbane_gryphon"},
    # Wilderness
    {"key": "chaos_elemental", "label": "Chaos Elemental", "category": "wilderness",
     "kc_npcs": ["Chaos Elemental"], "wom_metric": "chaos_elemental"},
    {"key": "venenatis_spindel", "label": "Venenatis / Spindel", "category": "wilderness",
     "kc_npcs": ["Venenatis", "Spindel"], "wom_metric": "venenatis",
     "ca_monsters": ["Venenatis"]},
    {"key": "callisto_artio", "label": "Callisto / Artio", "category": "wilderness",
     "kc_npcs": ["Callisto", "Artio"], "wom_metric": "callisto", "ca_monsters": ["Callisto"]},
    {"key": "vetion_calvarion", "label": "Vet'ion / Calvar'ion", "category": "wilderness",
     "kc_npcs": ["Vet'ion", "Calvar'ion"], "wom_metric": "vetion", "ca_monsters": ["Vet'ion"],
     "pet_rate": 1 / 1500},
    {"key": "king_black_dragon", "label": "King Black Dragon", "category": "wilderness",
     "kc_npcs": ["King Black Dragon"], "wom_metric": "king_black_dragon", "pet_rate": 1 / 3000},
    {"key": "wilderness_demi_bosses", "label": "Chaos Fanatic, Crazy Archaeologist & Scorpia",
     "short": "Wilderness demi-boss", "category": "wilderness",
     "kc_npcs": ["Chaos Fanatic", "Crazy archaeologist", "Scorpia"],
     "wom_metric": "chaos_fanatic",
     "ca_monsters": ["Chaos Fanatic", "Crazy Archaeologist", "Scorpia"]},
    # Group content
    {"key": "barrows", "label": "Barrows", "category": "group", "kc_npcs": ["Barrows"],
     "wom_metric": "barrows_chests", "unit": "chests"},
    {"key": "dagannoth_kings", "label": "Dagannoth Kings", "category": "group",
     "kc_npcs": ["Dagannoth Rex", "Dagannoth Prime", "Dagannoth Supreme"],
     "wom_metric": "dagannoth_rex",
     "ca_monsters": ["Dagannoth Rex", "Dagannoth Prime", "Dagannoth Supreme"],
     "unique_rate": 1 / 43, "conditional": True, "pet_rate": 1 / 5000},
    {"key": "moons_of_peril", "label": "Moons of Peril", "category": "group",
     "kc_npcs": ["Lunar Chest"], "wom_metric": "lunar_chests",
     "ca_monsters": ["Moons of Peril"], "unique_rate": 1 / 40, "conditional": True,
     "unit": "chests"},
    # Skilling bosses
    {"key": "zalcano", "label": "Zalcano", "category": "skilling", "kc_npcs": ["Zalcano"],
     "kph": 20.0},
)

ENCOUNTER_BY_KEY = {e["key"]: e for e in ENCOUNTERS}

#: Efficient-ish XP/hour for a mid-level account. Rough on purpose — it sizes
#: "N XP" targets, nobody is scored on it. Hitpoints/Farming are left out
#: (passive / timer-gated).
SKILL_XP_RATES = {
    "Attack": 110_000, "Strength": 120_000, "Defence": 110_000, "Ranged": 150_000,
    "Magic": 180_000, "Prayer": 400_000, "Cooking": 350_000, "Woodcutting": 90_000,
    "Fletching": 400_000, "Fishing": 80_000, "Firemaking": 300_000, "Crafting": 250_000,
    "Smithing": 250_000, "Mining": 70_000, "Herblore": 300_000, "Agility": 55_000,
    "Thieving": 200_000, "Slayer": 55_000, "Runecraft": 60_000, "Hunter": 120_000,
    "Construction": 600_000,
}

#: Rough team-hours to finish one CA of a tier at a boss you can already kill.
CA_TIER_HOURS = {"Easy": 0.3, "Medium": 0.6, "Hard": 1.5, "Elite": 3.0,
                 "Master": 6.0, "Grandmaster": 12.0}

SLAYER_TASK_HOURS = 1.0
LOOT_GP_PER_HOUR = 1_500_000

# --------------------------------------------------------------------------- #
# Sizing helpers
# --------------------------------------------------------------------------- #


def capacity_hours(team_size: int, days: float, activity: str = "normal") -> float:
    """Team-hours available over the event: members × days × daily play."""
    per_day = ACTIVITY_HOURS_PER_DAY.get(activity, ACTIVITY_HOURS_PER_DAY["normal"])
    return max(1, int(team_size or 1)) * max(0.5, float(days or 1)) * per_day


def tier_bounds(capacity: float) -> dict[str, tuple[float, float]]:
    """``{tier: (lower_hours, upper_hours)}`` — contiguous bands, each tier's
    lower edge being the previous tier's upper edge (Easy starts at 0)."""
    out: dict[str, tuple[float, float]] = {}
    lower = 0.0
    for tier in DIFFICULTIES:
        upper = max(TIER_SHARE[tier] * capacity, TIER_FLOOR_HOURS[tier])
        upper = max(upper, lower * 1.5)  # keep bands strictly increasing
        out[tier] = (lower, upper)
        lower = upper
    return out


def tier_for_hours(hours: float, bounds: dict) -> Optional[str]:
    """The tier whose band holds ``hours``; None when it's above Elite."""
    if hours is None or hours <= 0 or not math.isfinite(hours):
        return None
    for tier in DIFFICULTIES:
        lo, hi = bounds[tier]
        if hours <= hi:
            return tier
    return None


def tier_target_hours(tier: str, bounds: dict) -> float:
    """The sizing aim inside a tier: the geometric middle of its band (Easy
    aims at half its upper edge, since its band starts at 0)."""
    lo, hi = bounds[tier]
    if lo <= 0:
        return hi / 2
    return math.sqrt(lo * hi)


_NICE_STEPS = (1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5)


def nice_round(value: float) -> int:
    """Round to a number people write on a bingo tile: 1-10 exactly, then
    1/1.5/2/2.5/3/4/5/6/7.5 × 10ⁿ."""
    if value <= 1:
        return 1
    if value < 10:
        return int(round(value))
    exp = 10 ** int(math.floor(math.log10(value)))
    mant = value / exp
    best = min(_NICE_STEPS + (10,), key=lambda s: abs(math.log(s) - math.log(mant)))
    return int(round(best * exp))


def format_count(n: int) -> str:
    """1,500 → "1.5k", 2,000,000 → "2m" (XP/GP labels)."""
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:g}m"
    if n >= 10_000:
        return f"{n / 1000:g}k"
    return f"{n:,}"


def apportion(count: int, weights: dict[str, float]) -> dict[str, int]:
    """Split ``count`` across tiers by weight (largest remainder)."""
    live = {t: max(0.0, float(weights.get(t) or 0)) for t in DIFFICULTIES}
    total = sum(live.values())
    if count <= 0:
        return {t: 0 for t in DIFFICULTIES}
    if total <= 0:
        live = {t: 1.0 for t in DIFFICULTIES}
        total = 4.0
    raw = {t: count * w / total for t, w in live.items()}
    out = {t: int(math.floor(v)) for t, v in raw.items()}
    left = count - sum(out.values())
    for t in sorted(DIFFICULTIES, key=lambda t: raw[t] - out[t], reverse=True)[:left]:
        out[t] += 1
    return out


# --------------------------------------------------------------------------- #
# Candidate builders
# --------------------------------------------------------------------------- #
# A candidate is a plain dict:
#   key         stable identity ("kc:zulrah:earth") — rerolls exclude by it
#   family      diversity bucket ("enc:zulrah", "skill:Magic", "slayer", "loot")
#   encounter   encounter key or None
#   category    CONTENT_CATEGORIES key
#   kind        TASK_KINDS key
#   hours       estimated team-hours
#   estimated   True when a rate came from our own fallback, not WOM/wiki
#   detail      one line on where the estimate comes from
#   task        the POST /events/{id}/tasks payload (sans difficulty/points)


def _rate_text(rate: float) -> str:
    return f"1/{round(1 / rate):,}" if rate > 0 else "?"


def _kills_word(enc: dict, n: int) -> str:
    unit = enc.get("unit") or "kills"
    if n == 1:
        return {"kills": "kill", "completions": "completion", "chests": "chest"}.get(unit, unit)
    return unit


def _name(enc: dict) -> str:
    return enc.get("short") or enc["label"]


def _bare(enc: dict) -> str:
    """The name without a leading "The", for "Any Leviathan unique" /
    "5 Hueycoatl kills" (but "a CA at The Leviathan" keeps it)."""
    name = _name(enc)
    return name[4:] if name.startswith("The ") else name


def _item_label(enc: dict, item: str) -> str:
    """"Dragon pickaxe from Kalphite Queen", but plain "Vorkath's head" when
    the item already names its boss."""
    first = _name(enc).split()[0].strip("'").lower()
    if first in ("the", "a") and len(_name(enc).split()) > 1:
        first = _name(enc).split()[1].lower()
    return item if first in item.lower() else f"{item} from {_name(enc)}"


def _kc_task(enc: dict, n: int) -> dict:
    npcs = list(enc["kc_npcs"])
    task = {"type": "kc_target", "label": f"{n:,} {_bare(enc)} {_kills_word(enc, n)}",
            "target": npcs[0], "target_value": n}
    if len(npcs) > 1:
        task["config"] = {"npcs": npcs}
    return task


def _item_lock(enc: dict, names: Iterable[str]) -> dict:
    """``item_npcs`` restricting each listed item to this encounter's NPCs, so
    "any Callisto unique" isn't satisfied by a Dragon pickaxe from elsewhere."""
    npcs = list(enc["kc_npcs"])
    return {n: npcs for n in names}


def encounter_candidates(enc: dict, bounds: dict) -> list[dict]:
    """Every sized candidate one assembled encounter can offer."""
    out: list[dict] = []
    kph = float(enc.get("kph") or 0)
    if kph <= 0:
        return out
    base = {"encounter": enc["key"], "family": f"enc:{enc['key']}",
            "category": enc["category"]}
    est = bool(enc.get("kph_estimated"))
    rate_note = f"{kph:g}/hr"

    # Kill counts — one per tier, sized to the tier's aim.
    for tier in DIFFICULTIES:
        n = nice_round(tier_target_hours(tier, bounds) * kph)
        hours = n / kph
        if tier_for_hours(hours, bounds) != tier:
            continue
        out.append({**base, "key": f"kc:{enc['key']}:{n}", "kind": "kc", "hours": hours,
                    "estimated": est, "detail": f"{n:,} at ~{rate_note}",
                    "task": _kc_task(enc, n)})

    # Any unique — 1..5 of them, whichever counts land in a tier. A one-item
    # table is the specific-item task below ("Any Scurrius unique" isn't).
    uniques = [u["name"] for u in enc.get("uniques") or []]
    p_any = float(enc.get("unique_rate") or 0)
    if len(uniques) > 1 and p_any > 0:
        seen_n: set[int] = set()
        for tier in DIFFICULTIES:
            # "5 KBD uniques" off a 3-item table reads as a grind, not a goal.
            n_max = min(5, max(2, len(uniques)))
            n = max(1, min(n_max, int(round(tier_target_hours(tier, bounds) * p_any * kph))))
            if n in seen_n:
                continue
            hours = n / (p_any * kph)
            if tier_for_hours(hours, bounds) != tier:
                continue
            seen_n.add(n)
            label = (f"Any {_bare(enc)} unique" if n == 1
                     else f"{n} {_bare(enc)} uniques")
            out.append({
                **base, "key": f"unique_any:{enc['key']}:{n}", "kind": "uniques",
                "hours": hours, "estimated": est or bool(enc.get("unique_rate_estimated")),
                "detail": f"any of {len(uniques)} uniques, ~{_rate_text(p_any)} per "
                          f"{_kills_word(enc, 1)} at {rate_note}",
                "task": {"type": "item_collection", "label": label, "target_value": n,
                         "config": {"kind": "any_of",
                                    "items": [{"item_name": u} for u in uniques],
                                    "item_npcs": _item_lock(enc, uniques)}},
            })

    # A specific unique — only where the per-item rate is real.
    if not enc.get("conditional"):
        for u in enc.get("uniques") or []:
            rate = float(u.get("rate") or 0)
            if rate <= 0:
                continue
            hours = 1 / (rate * kph)
            if tier_for_hours(hours, bounds) is None:
                continue
            out.append({
                **base, "key": f"unique:{enc['key']}:{u['name'].lower()}", "kind": "uniques",
                "hours": hours, "estimated": est,
                "detail": f"{_rate_text(rate)} per {_kills_word(enc, 1)} at {rate_note}",
                "task": {"type": "item_collection", "label": _item_label(enc, u["name"]),
                         "target": u["name"],
                         "target_value": 1,
                         "config": {"source_npcs": list(enc["kc_npcs"])}},
            })

    # Pet.
    pet = enc.get("pet")
    pet_rate = float(enc.get("pet_rate") or 0)
    if pet and pet_rate > 0:
        hours = 1 / (pet_rate * kph)
        if tier_for_hours(hours, bounds) is not None:
            out.append({**base, "key": f"pet:{enc['key']}", "kind": "pets", "hours": hours,
                        "estimated": est,
                        "detail": f"{_rate_text(pet_rate)} per {_kills_word(enc, 1)} at {rate_note}",
                        "task": {"type": "pet_collection", "label": pet, "target": pet,
                                 "target_value": 1}})

    # Combat achievements — one tier per candidate.
    monsters = enc.get("ca_monsters_resolved") or []
    for ca_tier in enc.get("ca_tiers") or []:
        hours = CA_TIER_HOURS.get(ca_tier)
        if not hours or tier_for_hours(hours, bounds) is None:
            continue
        article = "An" if ca_tier[0] in "AEIOU" else "A"
        out.append({**base, "key": f"ca:{enc['key']}:{ca_tier.lower()}", "kind": "ca",
                    "hours": hours, "estimated": True,
                    "detail": f"typical time for {article.lower()} {ca_tier} task",
                    "task": {"type": "ca_target",
                             "label": f"{article} {ca_tier} combat achievement at {_name(enc)}",
                             "target_value": 1,
                             "config": {"monsters": monsters, "tiers": [ca_tier]}}})
    return out


def general_candidates(bounds: dict) -> list[dict]:
    """Skill XP, slayer task and loot-value candidates, sized per tier."""
    out: list[dict] = []
    for skill, rate in SKILL_XP_RATES.items():
        for tier in DIFFICULTIES:
            xp = nice_round(tier_target_hours(tier, bounds) * rate)
            xp = max(xp, 10_000)
            hours = xp / rate
            if tier_for_hours(hours, bounds) != tier:
                continue
            out.append({"key": f"xp:{skill.lower()}:{xp}", "family": f"skill:{skill}",
                        "encounter": None, "category": "skilling", "kind": "xp",
                        "hours": hours, "estimated": True,
                        "detail": f"~{format_count(rate)} XP/hr",
                        "task": {"type": "xp_target",
                                 "label": f"{format_count(xp)} {skill} XP",
                                 "target": skill, "target_value": xp}})
    for tier in DIFFICULTIES:
        n = nice_round(tier_target_hours(tier, bounds) / SLAYER_TASK_HOURS)
        hours = n * SLAYER_TASK_HOURS
        if tier_for_hours(hours, bounds) == tier and n <= 250:
            out.append({"key": f"slayer:{n}", "family": "slayer", "encounter": None,
                        "category": "slayer", "kind": "slayer", "hours": hours,
                        "estimated": True, "detail": "~1 hour per task",
                        "task": {"type": "slayer_target",
                                 "label": f"{n} slayer task{'s' if n != 1 else ''}",
                                 "target_value": n}})
        gp = nice_round(tier_target_hours(tier, bounds) * LOOT_GP_PER_HOUR)
        gp = max(gp, 500_000)
        hours = gp / LOOT_GP_PER_HOUR
        if tier_for_hours(hours, bounds) == tier:
            out.append({"key": f"loot:{gp}", "family": "loot", "encounter": None,
                        "category": "general", "kind": "loot", "hours": hours,
                        "estimated": True, "detail": f"~{format_count(LOOT_GP_PER_HOUR)} GP/hr",
                        "task": {"type": "loot_value",
                                 "label": f"{format_count(gp)} GP of loot",
                                 "target_value": gp}})
    return out


# --------------------------------------------------------------------------- #
# Library tasks
# --------------------------------------------------------------------------- #
# Public (and the clan's own private) rows of ``web_event_task_library`` join
# the pool when they can be sized: every item they name is a unique of one
# assembled encounter (or the boss / pet / skill they target is one we price).
# The task goes in exactly as the library holds it; only difficulty and points
# are ours. A row several bosses could satisfy (a Dragon pickaxe) yields one
# candidate per boss, sharing a "lib:<id>" signature so only one is picked.

#: Library tasks get a nudge over generated ones: a clan has already run them
#: and their names have more character than "3 Zulrah uniques".
LIBRARY_WEIGHT = 1.5

#: Exact all-of odds by inclusion-exclusion up to this many items (2^n terms).
_ALL_OF_EXACT_MAX = 12

#: Config keys that change how a task reports, not what it counts.
_COSMETIC_CONFIG_KEYS = {"progress_notify"}

#: Below these a library XP / loot row is a typo, not a goal (a "2,000,000
#: Fishing XP" row that actually asks for 2 Attack XP exists).
_MIN_LIBRARY_XP = 10_000
_MIN_LIBRARY_GP = 100_000


def _norm(name) -> str:
    return " ".join(str(name or "").lower().split())


def _config_items(config: dict) -> list[tuple[str, float]]:
    """``[(lower name, points)]`` from a flat ``items`` list (dicts or bare
    strings). Empty for nested shapes (groups / paths), which aren't sized."""
    out = []
    for it in config.get("items") or []:
        if isinstance(it, str):
            out.append((_norm(it), 0.0))
        elif isinstance(it, dict):
            name = it.get("item_name") or it.get("name")
            if name:
                try:
                    pts = float(it.get("points") or 0)
                except (TypeError, ValueError):
                    pts = 0.0
                out.append((_norm(name), pts))
    return out


def item_rates(enc: dict) -> dict[str, float]:
    """``{lower item name: per-kill chance}`` for one assembled encounter.

    Where the wiki rates are conditional (raids, DKs, Moons) they are shares of
    the unique roll, so they are rescaled to sum to ``unique_rate``. Items
    without a rate are left out, so a task naming one can't be sized."""
    uniques = enc.get("uniques") or []
    rated = {_norm(u["name"]): float(u["rate"]) for u in uniques if u.get("rate")}
    if enc.get("conditional"):
        p_any = float(enc.get("unique_rate") or 0)
        total = sum(rated.values())
        if p_any <= 0 or total <= 0:
            return {}
        return {n: p_any * r / total for n, r in rated.items()}
    return rated


def _expected_all_of(rates: list[float]) -> float:
    """Expected kills to see every item at least once (each an independent
    per-kill chance; continuous-time approximation)."""
    m = len(rates)
    if m <= _ALL_OF_EXACT_MAX:
        total = 0.0
        for mask in range(1, 1 << m):
            s = sum(rates[i] for i in range(m) if mask >> i & 1)
            total += (1 if bin(mask).count("1") % 2 else -1) / s
        return total
    mean = sum(rates) / m
    return sum(1 / k for k in range(1, m + 1)) / mean


def _expected_distinct(rates: list[float], n: int) -> float:
    """Expected kills to see ``n`` different items (equal-rate approximation)."""
    m = len(rates)
    mean = sum(rates) / m
    return sum(1 / ((m - k) * mean) for k in range(min(n, m)))


def _library_kills(task: dict, rates: dict[str, float]) -> Optional[float]:
    """Expected kills for an item_collection task at a boss with these item
    rates, or None when any item it names isn't one of the boss's uniques."""
    config = task.get("config") or {}
    kind = config.get("kind")
    n = int(task.get("target_value") or 1)
    if not kind:
        r = rates.get(_norm(task.get("target")))
        return n / r if r else None
    items = _config_items(config)
    if not items or any(name not in rates for name, _p in items):
        return None
    rs = [rates[name] for name, _p in items]
    if kind == "any_of":
        return n / sum(rs)
    if kind == "any_of_distinct":
        return _expected_distinct(rs, n)
    if kind in ("all_of", "assembly"):
        return _expected_all_of(rs)
    if kind == "point_collection":
        per_kill = sum(rates[name] * p for name, p in items)
        return n / per_kill if per_kill > 0 else None
    return None


def _restricted_npcs(config: dict) -> set[str]:
    """Every NPC a library task's source restriction names (lower-cased)."""
    out = {_norm(n) for n in config.get("source_npcs") or []}
    for npcs in (config.get("item_npcs") or {}).values():
        out |= {_norm(n) for n in npcs or []}
    return out


def library_candidates(library: Iterable[dict], catalog: Iterable[dict]) -> list[dict]:
    """Sized candidates from task-library rows (unsized-by-tier: the caller
    tags difficulty). Rows that can't be tied to a rate are skipped."""
    catalog = list(catalog)
    out: list[dict] = []
    rates_by_enc = {e["key"]: item_rates(e) for e in catalog}
    for row in library:
        task = {"type": row.get("type"), "label": str(row.get("name") or "").strip(),
                "target": row.get("target") or None,
                "target_value": row.get("target_value"), "config": row.get("config") or None}
        if not task["label"] or not task["type"]:
            continue
        config = task["config"] if isinstance(task["config"], dict) else {}
        if task["config"] is not None and not isinstance(task["config"], dict):
            continue
        counted = {k: v for k, v in config.items() if k not in _COSMETIC_CONFIG_KEYS}
        lib_id = row.get("id")
        base = {"library_id": lib_id, "estimated": True, "task": task,
                "lib_sig": f"lib:{lib_id}"}
        n = task["target_value"]
        n = int(n) if isinstance(n, (int, float)) and n > 0 else 1
        ttype = task["type"]

        if ttype == "xp_target":
            skill = next((k for k in SKILL_XP_RATES if _norm(k) == _norm(task["target"])), None)
            if skill and not counted and n >= _MIN_LIBRARY_XP:
                rate = SKILL_XP_RATES[skill]
                out.append({**base, "key": f"lib:{lib_id}", "family": f"skill:{skill}",
                            "encounter": None, "category": "skilling", "kind": "xp",
                            "hours": n / rate, "detail": f"~{format_count(rate)} XP/hr"})
            continue
        if ttype == "loot_value":
            if not counted and n >= _MIN_LIBRARY_GP:
                out.append({**base, "key": f"lib:{lib_id}", "family": "loot",
                            "encounter": None, "category": "general", "kind": "loot",
                            "hours": n / LOOT_GP_PER_HOUR,
                            "detail": f"~{format_count(LOOT_GP_PER_HOUR)} GP/hr"})
            continue

        restricted = _restricted_npcs(config)
        for enc in catalog:
            kph = float(enc.get("kph") or 0)
            if kph <= 0:
                continue
            kc_npcs = {_norm(x) for x in enc["kc_npcs"]}
            if restricted and not restricted & kc_npcs:
                continue
            kills, kind = None, None
            if ttype == "kc_target":
                npcs = {_norm(x) for x in config.get("npcs") or []} or {_norm(task["target"])}
                if npcs and npcs <= kc_npcs:
                    kills, kind = n, "kc"
            elif ttype == "pet_collection":
                pet, rate = enc.get("pet"), float(enc.get("pet_rate") or 0)
                if pet and rate > 0 and _norm(task["target"]) == _norm(pet):
                    kills, kind = n / rate, "pets"
            elif ttype == "item_collection":
                kills = _library_kills(task, rates_by_enc[enc["key"]])
                kind = "uniques"
            if not kills:
                continue
            out.append({**base, "key": f"lib:{lib_id}:{enc['key']}",
                        "family": f"enc:{enc['key']}", "encounter": enc["key"],
                        "category": enc["category"], "kind": kind, "hours": kills / kph,
                        "estimated": bool(enc.get("kph_estimated")),
                        "detail": f"library task, ~{kills:,.0f} {enc.get('unit') or 'kills'} "
                                  f"at {kph:g}/hr"})
    return out


def task_signatures(c: dict) -> set[str]:
    """What a candidate asks for, independent of its label: two candidates
    sharing a signature are the same task (a library "Trident of the Seas" and
    a generated "Trident of the seas from Kraken"), so only one is picked."""
    if "sigs" in c:
        return c["sigs"]
    task = c["task"]
    config = task.get("config") if isinstance(task.get("config"), dict) else {}
    tv = task.get("target_value") or 1
    ttype = task.get("type")
    kind = config.get("kind")
    sigs: set[str] = set()
    if c.get("lib_sig"):
        sigs.add(c["lib_sig"])
    if ttype == "item_collection":
        names = sorted({name for name, _p in _config_items(config)})
        if not kind:
            sigs.add(f"item:{_norm(task.get('target'))}:{tv}")
        elif kind == "any_of" and len(names) == 1:
            sigs.add(f"item:{names[0]}:{tv}")
        elif names:
            sigs.add(f"{kind}:{'|'.join(names)}:{tv}")
    elif ttype == "kc_target":
        npcs = sorted({_norm(x) for x in config.get("npcs") or []} or {_norm(task.get("target"))})
        sigs.add(f"kc:{'|'.join(npcs)}:{tv}")
    elif ttype in ("pet_collection", "xp_target"):
        sigs.add(f"{ttype}:{_norm(task.get('target'))}:{tv}")
    elif ttype in ("loot_value", "slayer_target"):
        sigs.add(f"{ttype}:{tv}")
    c["sigs"] = sigs
    return sigs


def build_candidates(catalog: Iterable[dict], bounds: dict,
                     library: Iterable[dict] = ()) -> list[dict]:
    """The whole sized pool, each tagged with its tier."""
    catalog = list(catalog)
    pool: list[dict] = []
    for enc in catalog:
        pool.extend(encounter_candidates(enc, bounds))
    pool.extend(general_candidates(bounds))
    pool.extend(library_candidates(library, catalog))
    for c in pool:
        c["difficulty"] = tier_for_hours(c["hours"], bounds)
    return [c for c in pool if c["difficulty"]]


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def _family_cap(family: str) -> int:
    return MAX_PER_FAMILY["encounter" if family.startswith("enc:") else
                          "skill" if family.startswith("skill:") else family]


def _clan_factor(c: dict, focus: str, activity: dict, roster: int) -> float:
    """Weight from how many members were active at the boss lately."""
    if focus == "off" or not c.get("encounter"):
        return 1.0
    players = int(activity.get(c["encounter"]) or 0)
    # Share of a typical roster, so a 20-person clan with 10 Zulrah regulars
    # reads as "very familiar" just like a 400-person clan with 200.
    share = min(1.0, players / max(3.0, roster * 0.25))
    if focus == "familiar":
        return 0.25 + 3.0 * share
    return 1.6 - 1.4 * share  # fresh


def select(pool: list[dict], *, count: int, mix: dict, rng: random.Random,
           must_include: Iterable[str] = (), taken: Iterable[dict] = (),
           clan_focus: str = "off", activity: Optional[dict] = None,
           roster: int = 0, only_tier: Optional[str] = None) -> tuple[list[dict], dict]:
    """Pick ``count`` candidates from ``pool``.

    ``taken`` are candidates already on the board (a reroll keeps the rest):
    they count toward diversity but not toward ``count``. ``only_tier`` pins
    every pick to one tier (a single-cell reroll keeps its difficulty).
    Returns ``(picks, shortfall)`` where shortfall maps tier → slots that had
    to borrow from a neighbouring tier (or could not be filled at all under
    key ``"unfilled"``).
    """
    activity = activity or {}
    family_uses: dict[str, int] = {}
    category_uses: dict[str, int] = {}
    kind_uses: dict[str, int] = {}
    used_keys: set[str] = set()
    used_labels: set[str] = set()
    used_sigs: set[str] = set()

    def _note(c: dict) -> None:
        family_uses[c["family"]] = family_uses.get(c["family"], 0) + 1
        category_uses[c["category"]] = category_uses.get(c["category"], 0) + 1
        kind_uses[c["kind"]] = kind_uses.get(c["kind"], 0) + 1
        used_keys.add(c["key"])
        used_labels.add(c["task"]["label"].strip().lower())
        used_sigs.update(task_signatures(c))

    for c in taken:
        _note(c)

    def _weight(c: dict) -> float:
        """Within-kind weight: diversity + clan focus. The kind's own share
        is applied in ``_pick_from``."""
        if c["key"] in used_keys or c["task"]["label"].strip().lower() in used_labels:
            return 0.0
        if task_signatures(c) & used_sigs:
            return 0.0
        fam = family_uses.get(c["family"], 0)
        if fam >= _family_cap(c["family"]):
            return 0.0
        w = 0.25 ** fam
        w /= 1.0 + 0.35 * category_uses.get(c["category"], 0)
        w *= _clan_factor(c, clan_focus, activity, roster)
        if c.get("library_id") is not None:
            w *= LIBRARY_WEIGHT
        return w

    by_tier: dict[str, list[dict]] = {t: [] for t in DIFFICULTIES}
    for c in pool:
        by_tier[c["difficulty"]].append(c)

    def _pick_from(cands: list[dict]) -> Optional[dict]:
        raw = [_weight(c) for c in cands]
        kind_total: dict[str, float] = {}
        for c, w in zip(cands, raw):
            kind_total[c["kind"]] = kind_total.get(c["kind"], 0.0) + w
        weights = [
            (w / kind_total[c["kind"]]) * KIND_WEIGHT.get(c["kind"], 1.0)
            / (1.0 + 0.3 * kind_uses.get(c["kind"], 0)) if w > 0 else 0.0
            for c, w in zip(cands, raw)
        ]
        total = sum(weights)
        if total <= 0:
            return None
        r = rng.random() * total
        for c, w in zip(cands, weights):
            r -= w
            if r <= 0 and w > 0:
                return c
        return next(c for c, w in zip(reversed(cands), reversed(weights)) if w > 0)

    quotas = ({t: (count if t == only_tier else 0) for t in DIFFICULTIES}
              if only_tier else apportion(count, mix))
    picks: list[dict] = []
    shortfall: dict[str, int] = {}

    def _take(c: dict) -> None:
        picks.append(c)
        _note(c)

    # Must-include bosses first, each into the tier with the most room.
    for enc_key in must_include:
        if sum(quotas.values()) <= 0:
            break
        tiers = sorted((t for t in DIFFICULTIES if quotas[t] > 0),
                       key=lambda t: quotas[t], reverse=True)
        for t in tiers:
            c = _pick_from([x for x in by_tier[t] if x.get("encounter") == enc_key])
            if c:
                _take(c)
                quotas[t] -= 1
                break

    # Scarce tiers first: Elite pools are the thinnest.
    for t in reversed(DIFFICULTIES):
        while quotas[t] > 0:
            c = _pick_from(by_tier[t])
            if c is None and not only_tier:
                # Borrow from the nearest tier, easier first.
                idx = DIFFICULTIES.index(t)
                order = sorted((x for x in DIFFICULTIES if x != t),
                               key=lambda x: (abs(DIFFICULTIES.index(x) - idx),
                                              DIFFICULTIES.index(x) > idx))
                for alt in order:
                    c = _pick_from(by_tier[alt])
                    if c:
                        break
                if c:
                    shortfall[t] = shortfall.get(t, 0) + 1
            if c is None:
                shortfall["unfilled"] = shortfall.get("unfilled", 0) + quotas[t]
                quotas[t] = 0
                break
            _take(c)
            quotas[t] -= 1
    return picks, shortfall


def finalize(c: dict) -> dict:
    """The response row: the task payload with difficulty + default points,
    plus the explanation fields the preview renders."""
    # Absent, not null: the web's EventTaskInput has target/target_value as
    # optional strings/ints, so one library row with no target (an item list,
    # a loot_value total) failed the whole preview there.
    task = {k: v for k, v in c["task"].items() if v is not None}
    # The web's EventTaskInput carries config as a JSON string (the task
    # routes accept either form).
    if task.get("config") is not None:
        task["config"] = json.dumps(task["config"], sort_keys=True)
    task["difficulty"] = c["difficulty"]
    task["points"] = DEFAULT_POINTS[c["difficulty"]]
    return {
        "key": c["key"],
        "kind": c["kind"],
        "category": c["category"],
        "encounter": c.get("encounter"),
        "source": "library" if c.get("library_id") is not None else "generated",
        "difficulty": c["difficulty"],
        "hours": round(c["hours"], 2),
        "estimated": bool(c.get("estimated")),
        "detail": c.get("detail") or "",
        "task": task,
    }


def filter_pool(pool: list[dict], *, categories: Optional[set] = None,
                kinds: Optional[set] = None, exclude_keys: Iterable[str] = (),
                exclude_encounters: Iterable[str] = (),
                exclude_labels: Iterable[str] = ()) -> list[dict]:
    """Drop what the criteria rule out. ``must_include`` bosses survive a
    category filter (asking for Zulrah by name beats unticking Bosses)."""
    ex_keys = set(exclude_keys)
    ex_enc = set(exclude_encounters)
    ex_labels = {str(l).strip().lower() for l in exclude_labels}
    out = []
    for c in pool:
        if c["key"] in ex_keys or (c.get("encounter") in ex_enc):
            continue
        if c["task"]["label"].strip().lower() in ex_labels:
            continue
        if categories is not None and c["category"] not in categories:
            continue
        if kinds is not None and c["kind"] not in kinds:
            continue
        out.append(c)
    return out


def generate(catalog: list[dict], *, count: int, capacity: float, mix: dict,
             seed: int, categories: Optional[set] = None, kinds: Optional[set] = None,
             must_include: Iterable[str] = (), exclude_keys: Iterable[str] = (),
             exclude_encounters: Iterable[str] = (), exclude_labels: Iterable[str] = (),
             taken_keys: Iterable[str] = (), clan_focus: str = "off",
             activity: Optional[dict] = None, roster: int = 0,
             only_tier: Optional[str] = None, library: Iterable[dict] = ()) -> dict:
    """Top-level entry: size the pool for ``capacity`` and pick ``count``.

    ``library`` is the task-library rows the clan can see (see
    :func:`library_candidates`). Deterministic for a given (catalog, library,
    criteria, seed) so a shared seed reproduces the same board."""
    bounds = tier_bounds(capacity)
    full = build_candidates(catalog, bounds, library)
    by_key = {c["key"]: c for c in full}
    must = [k for k in must_include if k in ENCOUNTER_BY_KEY or any(
        e.get("key") == k for e in catalog)]
    # A named boss survives the category/kind filters.
    filtered = filter_pool(full, categories=categories, kinds=kinds,
                           exclude_keys=exclude_keys, exclude_encounters=exclude_encounters,
                           exclude_labels=exclude_labels)
    if must:
        keep = {c["key"] for c in filtered}
        extra = [c for c in filter_pool(full, exclude_keys=exclude_keys,
                                        exclude_labels=exclude_labels)
                 if c.get("encounter") in must and c["key"] not in keep
                 and (kinds is None or c["kind"] in kinds)]
        filtered = filtered + extra
    taken = [by_key[k] for k in taken_keys if k in by_key]
    rng = random.Random(seed)
    picks, shortfall = select(filtered, count=min(max(0, count), MAX_GENERATE), mix=mix,
                              rng=rng, must_include=must, taken=taken,
                              clan_focus=clan_focus, activity=activity, roster=roster,
                              only_tier=only_tier)
    order = {t: i for i, t in enumerate(DIFFICULTIES)}
    picks.sort(key=lambda c: (order[c["difficulty"]], c["hours"]))
    rows = [finalize(c) for c in picks]
    return {
        "seed": seed,
        "capacity_hours": round(capacity, 1),
        "bounds": {t: [round(lo, 2), round(hi, 2)] for t, (lo, hi) in bounds.items()},
        "pool_size": len(filtered),
        "total_hours": round(sum(r["hours"] for r in rows), 1),
        "shortfall": shortfall,
        "tasks": rows,
    }
