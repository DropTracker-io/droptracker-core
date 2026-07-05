"""Pure planning helpers for the Hall of Fame service.

This module intentionally has no Discord, database or Redis imports so the
plan-building logic (which is where the historical ordering / duplication
bugs lived) can be unit-tested in isolation.  The Discord/DB orchestration
lives in ``services/hall_of_fame.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

# Logical message keys stored in group_personal_best_message.boss_name for the
# directory messages.  These values are already present in production rows, so
# they must not change.
DIRECTORY_KEY = "_hof_directory"
DIRECTORY_BOTTOM_KEY = "_hof_directory_bottom"

SEPULCHRE_CANONICAL = "Hallowed Sepulchre"
SEPULCHRE_FLOOR_RE = re.compile(r"^Hallowed Sepulchre Floor \d+$")

# Raid/boss variants that are combined into a single Hall of Fame message.
# NpcList carries both colon and colon-less spellings for raid modes, and
# group configs use whichever the NPC row happened to have — include both so
# grouping does not depend on punctuation.
RAID_GROUPS: Dict[str, List[str]] = {
    "Chambers of Xeric": [
        "Chambers of Xeric",
        "Chambers of Xeric: Challenge Mode",
        "Chambers of Xeric Challenge Mode",
    ],
    "Theatre of Blood": [
        "Theatre of Blood",
        "Theatre of Blood: Entry Mode",
        "Theatre of Blood Entry Mode",
        "Theatre of Blood: Hard Mode",
        "Theatre of Blood Hard Mode",
    ],
    "Tombs of Amascut": [
        "Tombs of Amascut",
        "Tombs of Amascut: Entry Mode",
        "Tombs of Amascut Entry Mode",
        "Tombs of Amascut: Expert Mode",
        "Tombs of Amascut Expert Mode",
    ],
    "Nightmare of Ashihama": [
        "Nightmare",
        "The Nightmare",
        "Phosani's Nightmare",
        "Nightmare of Ashihama",
    ],
    "The Gauntlet": ["The Gauntlet", "The Corrupted Gauntlet"],
}
RAID_VARIANT_TO_CANONICAL: Dict[str, str] = {
    variant: canonical
    for canonical, variants in RAID_GROUPS.items()
    for variant in variants
}

# Discord Components V2: total rendered text per message is capped at 4000
# characters.  Leave headroom for the header/footer when fitting lists.
MAX_MESSAGE_TEXT_CHARS = 4000
SELECT_OPTIONS_PER_MENU = 25

SELECT_CUSTOM_ID_PREFIX = "hof_boss_select"


def canonical_display_name(boss_name: str) -> str:
    """Map a configured boss name to the display name of the message it lives in."""
    if SEPULCHRE_FLOOR_RE.match(boss_name):
        return SEPULCHRE_CANONICAL
    return RAID_VARIANT_TO_CANONICAL.get(boss_name, boss_name)


def parse_boss_list(config_value: str | None, long_value: str | None) -> List[str]:
    """Parse the ``personal_best_embed_boss_list`` config into a clean name list.

    The value is stored either in ``config_value`` or, for long lists, in
    ``long_value`` as a JSON-ish bracketed CSV (e.g. ``["Zulrah", "Vorkath"]``).
    Duplicates (case-insensitive) are dropped while preserving first-seen order.
    """
    raw = (config_value or "").strip()
    if len(raw) < 10:
        raw = (long_value or "").strip()
    if len(raw) < 10:
        return []
    parts = raw.replace("[", "").replace("]", "").split(",")
    seen: set[str] = set()
    names: List[str] = []
    for part in parts:
        name = part.strip().strip('"').strip("'").strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


@dataclass
class BossPlanEntry:
    """One Hall of Fame message: a boss, or a raid with all its configured modes."""
    display_name: str
    variant_names: List[str] = field(default_factory=list)
    grouped: bool = False


def build_boss_plan(boss_names: List[str]) -> List[BossPlanEntry]:
    """Collapse configured boss names into display entries, sorted alphabetically.

    Raid variants (and Hallowed Sepulchre floors) collapse into one grouped
    entry per canonical name; everything else becomes an individual entry.
    """
    entries: Dict[str, BossPlanEntry] = {}
    for boss in boss_names:
        display = canonical_display_name(boss)
        grouped = display in RAID_GROUPS or display == SEPULCHRE_CANONICAL
        entry = entries.get(display)
        if entry is None:
            entries[display] = BossPlanEntry(display, [boss], grouped)
        elif boss not in entry.variant_names:
            entry.variant_names.append(boss)
    return sorted(entries.values(), key=lambda e: e.display_name.casefold())


def build_message_plan(display_names: List[str], individual_messages: bool) -> List[str]:
    """Return the ordered list of logical message keys a group's channel should hold.

    Individual mode:   directory, then one message per boss (alphabetical),
                       then a second directory at the bottom of the channel.
    Directory-only:    a single directory message.
    """
    plan = [DIRECTORY_KEY]
    if individual_messages and display_names:
        plan.extend(display_names)
        plan.append(DIRECTORY_BOTTOM_KEY)
    return plan


def fit_directory_lines(linked_lines: List[str], plain_lines: List[str],
                        limit: int = 3600) -> List[str]:
    """Pick the richest directory body that fits in ``limit`` characters.

    Prefers jump-link lines, falls back to plain names, and finally truncates
    with an "…and N more" marker.  ``linked_lines`` and ``plain_lines`` must be
    parallel lists describing the same bosses.
    """
    def total(lines: List[str]) -> int:
        return sum(len(line) + 1 for line in lines)

    if total(linked_lines) <= limit:
        return list(linked_lines)
    if total(plain_lines) <= limit:
        return list(plain_lines)
    fitted: List[str] = []
    used = 0
    for line in plain_lines:
        if used + len(line) + 1 > limit - 40:
            fitted.append(f"-# …and {len(plain_lines) - len(fitted)} more")
            break
        fitted.append(line)
        used += len(line) + 1
    return fitted


def chunk_select_options(display_names: List[str],
                         per_menu: int = SELECT_OPTIONS_PER_MENU) -> List[List[str]]:
    """Split boss display names into select-menu-sized chunks (max 25 options each)."""
    return [display_names[i:i + per_menu] for i in range(0, len(display_names), per_menu)]


def select_menu_custom_id(group_id: int, chunk_index: int) -> str:
    return f"{SELECT_CUSTOM_ID_PREFIX}:{group_id}:{chunk_index}"


def parse_select_custom_id(custom_id: str) -> int | None:
    """Return the group_id encoded in a boss-select custom_id, or None."""
    if not custom_id or not custom_id.startswith(f"{SELECT_CUSTOM_ID_PREFIX}:"):
        return None
    parts = custom_id.split(":")
    if len(parts) < 3:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None
