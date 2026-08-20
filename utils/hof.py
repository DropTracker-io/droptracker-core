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

# Custom guild emoji (DropTracker primary guild). It has to be the full
# <:name:id> form: a bare ':Construction:' shortcode is only expanded by the
# Discord *composer*, so a bot posting it renders the literal text instead.
# Emoji in message content are resolved from the CDN by id, so this renders in
# every group's guild regardless of the HOF bot's membership there.
CONSTRUCTION_EMOJI = "<:Construction:1533062962418417704>"

# Footer appended to the LAST message of every Hall of Fame channel (the bottom
# directory in individual-boss mode, otherwise the single directory message).
# Players assume the Hall of Fame only tracks PBs set after they installed the
# plugin, so tell them how to backfill the ones they already have. Lives here,
# not in the service, so the exact wording is covered by the unit tests.
SYNC_NOTE_TEXT = (
    "-# **Note**: You can sync all of your existing Personal Bests here by doing the following:\n"
    f"-# 1. Build an `Adventure Log`  (min. 83 {CONSTRUCTION_EMOJI} )  inside of an "
    f"`Achievement Gallery`  (80 {CONSTRUCTION_EMOJI} )  in your Player-Owned House.\n"
    "-# 2. Open the Adventure Log, and click on the `Counters` tab. This will immediately "
    "send your stored times to the DropTracker."
)


def canonical_display_name(boss_name: str) -> str:
    """Map a configured boss name to the display name of the message it lives in.

    Spelling variants are honoured here too ('Corrupted Gauntlet' must land in
    the same grouped message as 'The Corrupted Gauntlet'), otherwise a group
    whose list carries both spellings renders the same boss twice.
    """
    if SEPULCHRE_FLOOR_RE.match(boss_name):
        return SEPULCHRE_CANONICAL
    for candidate in npc_name_candidates(boss_name):
        canonical = RAID_VARIANT_TO_CANONICAL.get(candidate)
        if canonical:
            return canonical
    return boss_name


def plan_dedupe_key(display_name: str) -> str:
    """Key under which plan entries merge: case- and 'The '-insensitive.

    'Whisperer' and 'The Whisperer' are the same boss; groups routinely have
    both spellings in their configured list, and each must not get its own
    Hall of Fame message.
    """
    key = display_name.strip().casefold()
    if key.startswith("the "):
        key = key[4:]
    return key


_MODE_SUFFIX_RE = re.compile(r"^(.*?):? (Entry|Hard|Expert|Challenge) Mode$")


def npc_name_candidates(name: str) -> List[str]:
    """NpcList lookup candidates for a configured boss name, exact form first.

    Group boss lists are user-entered / migrated and routinely miss the exact
    NpcList spelling — 'Leviathan' vs 'The Leviathan', 'Tombs of Amascut Expert
    Mode' vs 'Tombs of Amascut: Expert Mode'. Silently dropping those bosses
    from the Hall of Fame is far worse than a fuzzy match, so try the obvious
    spelling variants ("The " prefix added/stripped, raid-mode colon inserted/
    removed) in a deterministic order.
    """
    name = (name or "").strip()
    if not name:
        return []
    candidates = [name]
    if name.casefold().startswith("the "):
        candidates.append(name[4:])
    else:
        candidates.append(f"The {name}")
    mode_match = _MODE_SUFFIX_RE.match(name)
    if mode_match:
        base, mode = mode_match.group(1), mode_match.group(2)
        candidates.append(f"{base}: {mode} Mode")
        candidates.append(f"{base} {mode} Mode")
    seen: set[str] = set()
    unique: List[str] = []
    for candidate in candidates:
        if candidate.casefold() not in seen:
            seen.add(candidate.casefold())
            unique.append(candidate)
    return unique


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
        # Merge spelling variants of the same boss ('Whisperer' + 'The
        # Whisperer') into one entry; the first-seen display name is the label.
        key = plan_dedupe_key(display)
        entry = entries.get(key)
        if entry is None:
            entries[key] = BossPlanEntry(display, [boss], grouped)
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


# ── Bot-consolidation migration helpers ─────────────────────────────────────
# The Hall of Fame runs in two processes while the legacy HOF application is
# retired (see services/hall_of_fame.py). These two decisions are what keep
# that safe, so they live here where they can be tested without Discord.


def hof_owner_is_self(managed_by_core: bool, is_legacy: bool) -> bool:
    """Does THIS process own a group's Hall of Fame?

    Ownership is a strict partition: a group is served by the core bot once its
    ``hof_managed_by_core`` ratchet is set, and by the legacy application until
    then. Exactly one of the two processes may answer True for any group — two
    writers on one channel is what produced the duplicate-message races the
    reconciler was rewritten to fix.
    """
    return bool(managed_by_core) != bool(is_legacy)


def forwardable_refresh_payloads(raw_items: List) -> List[str]:
    """Filter a drained refresh batch down to what may be handed to the peer.

    Either process can pop a PB signal belonging to the other, so it offers the
    batch to its peer. Forwarding is capped at ONE hop: anything already tagged
    is dropped rather than sent back, otherwise a signal that neither process
    can place would bounce between the two queues forever. The periodic sweep
    is the backstop for whatever gets dropped here.
    """
    import json

    out: List[str] = []
    for item in raw_items:
        try:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            payload = json.loads(item)
            if not isinstance(payload, dict) or payload.get("fwd"):
                continue
            payload["fwd"] = 1
            out.append(json.dumps(payload))
        except Exception:
            continue
    return out
