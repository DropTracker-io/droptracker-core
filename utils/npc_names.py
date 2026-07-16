"""NPC name normalization — the single spelling-insensitive identity rule.

Different submission sources spell the same boss differently ("Tombs of
Amascut: Expert Mode" vs "Tombs of Amascut Expert Mode"; "Chambers of Xeric
(Challenge mode)" vs "... Challenge Mode"), which historically split one boss
across several ``npc_list`` rows (suggestion #50). Everything that matches or
groups NPCs by name should compare **slugs**, not raw strings.

The slug rule here MUST stay equivalent to ``web_api.common.slugify`` and the
front-end ``apps/web/lib/slug.ts`` — the nice-URL system resolves the same
slugs. This module is intentionally dependency-free (stdlib ``re`` only) so it
is importable from the bot stack, the intake processors, and web_api alike.
"""
from __future__ import annotations

import re

_NONALNUM = re.compile(r"[^a-z0-9]+")

#: Raid difficulty variants that share the base raid's wiki drop table.
#: Used to expand a boss "family" (base + modes) for drop-table fallback.
MODE_SUFFIXES = ("challenge-mode", "hard-mode", "expert-mode", "entry-mode")


def npc_slug(name: str | None) -> str:
    """lowercase → non-alphanumeric runs to '-' → trim leading/trailing '-'.

    ``"Chambers of Xeric (Challenge mode)"`` and
    ``"Chambers of Xeric Challenge Mode"`` both yield
    ``"chambers-of-xeric-challenge-mode"``.
    """
    if not name:
        return ""
    return _NONALNUM.sub("-", str(name).lower()).strip("-")


def strip_the(slug: str) -> str:
    """Drop a leading "the-": sources disagree on the article ("The Whisperer"
    vs "Whisperer", "The Hueycoatl" vs "Hueycoatl")."""
    return slug[4:] if slug.startswith("the-") and len(slug) > 4 else slug


#: Alternate boss names → canonical match key (both sides in the-stripped slug
#: space). The plugin PB path names the boss NPC while the drop path names the
#: activity, splitting one boss across unrelated names.
NPC_ALIASES = {
    "crystalline-hunllef": "gauntlet",           # The Gauntlet
    "corrupted-hunllef": "corrupted-gauntlet",   # The Corrupted Gauntlet
    # The Royal Titans is a duo encounter; the individual titans are tracked
    # under the single encounter "Royal Titans".
    "branda-the-fire-queen": "royal-titans",
    "eldric-the-ice-king": "royal-titans",
}

#: Multi-boss encounters where the plugin/source may name the individual boss
#: NPC that was killed, but we track everything under one encounter row. Unlike
#: the slug ``NPC_ALIASES`` above (which only helps when there is NO exact
#: npc_list row), these members DO have their own npc_list rows, so the rewrite
#: must happen up front — before the exact-name lookup — to land drops on the
#: encounter's npc_id and display name. Keyed/valued by display name.
ENCOUNTER_NAME_ALIASES = {
    "Branda the Fire Queen": "Royal Titans",
    "Eldric the Ice King": "Royal Titans",
}


def canonical_encounter_name(npc_name: str | None) -> str | None:
    """Rewrite an individual encounter-member boss name to its encounter name.

    Returns ``npc_name`` unchanged when it isn't a known encounter member.
    """
    if not npc_name:
        return npc_name
    return ENCOUNTER_NAME_ALIASES.get(str(npc_name).strip(), npc_name)


def npc_match_key(name_or_slug: str | None) -> str:
    """Spelling-, article- and alias-insensitive identity key for an NPC.

    ``"The Gauntlet"``, ``"Crystalline Hunllef"`` and ``"gauntlet"`` all map to
    ``"gauntlet"``. This is the ONLY value two NPC names should ever be
    compared by.
    """
    slug = npc_slug(name_or_slug)
    key = strip_the(slug)
    return NPC_ALIASES.get(key, key)


def npc_match_variants(name_or_slug: str | None) -> list[str]:
    """Every raw slug that shares this NPC's match key, for SQL ``IN`` matching
    (``slug_expr IN :variants``) — aliases can't be computed in SQL, so we
    expand them Python-side instead."""
    key = npc_match_key(name_or_slug)
    if not key:
        return []
    variants = [key, f"the-{key}"]
    for alias, canonical in NPC_ALIASES.items():
        if canonical == key:
            variants.extend((alias, f"the-{alias}"))
    return variants


def npc_base_slug(slug: str) -> str | None:
    """Base-raid slug for a mode-variant slug, else None.

    ``"tombs-of-amascut-expert-mode"`` → ``"tombs-of-amascut"``;
    ``"vorkath"`` → None (not a mode variant).
    """
    for suffix in MODE_SUFFIXES:
        marker = f"-{suffix}"
        if slug.endswith(marker) and len(slug) > len(marker):
            return slug[: -len(marker)]
    return None


def npc_family_tiers(name_or_slug: str | None) -> list[list[str]]:
    """Boss-family slug candidates as priority tiers, each a variant list for
    SQL ``IN`` matching. Used by the drop-table fallback: the wiki table often
    lives on one arbitrary family member (CoX table on the base raid, ToB's on
    Hard Mode).

    Tier 1: this boss itself (spelling / article / alias variants);
    Tier 2: the base raid (when this is a mode variant);
    Tier 3: mode siblings.
    """
    key = npc_match_key(name_or_slug)
    if not key:
        return []
    tiers = [npc_match_variants(key)]
    base = npc_base_slug(key) or key
    if base != key:
        tiers.append(npc_match_variants(base))
    sibling_variants: list[str] = []
    for suffix in MODE_SUFFIXES:
        sib = f"{base}-{suffix}"
        if sib != key:
            sibling_variants.extend(npc_match_variants(sib))
    if sibling_variants:
        tiers.append(sibling_variants)
    return tiers


_TEAM_TRAILERS = re.compile(r"\s*(players?|scaled|s)\s*$", re.IGNORECASE)


def sanitize_team_size(raw) -> str:
    """Normalize a PB team-size label to the canonical encodings.

    The adventure-log path captured raw fragments like ``"(2"``, ``"(2 players)"``,
    ``"5 s"`` / ``"5 scaled"`` — creating parallel PB boards for the same team
    size. Canonical forms: ``"Solo"``, ``"2"``…, ``"11-15"``, ``"6+"``, ``"24+"``.
    """
    s = str(raw if raw is not None else "").strip().strip("`").strip()
    s = s.lstrip("(").rstrip(")").strip()
    s = _TEAM_TRAILERS.sub("", s).strip()
    if s.lower() in ("", "solo", "0", "1"):
        return "Solo"
    return s


def npc_slug_sql_expr(column: str) -> str:
    """SQL computing ``npc_slug(column)`` (MariaDB/MySQL 8 REGEXP_REPLACE).

    ``column`` is interpolated verbatim — trusted column references only.
    Mirrors ``web_api.common.slug_sql_expr``; duplicated here so the intake
    path never imports the web_api package.
    """
    return f"TRIM(BOTH '-' FROM REGEXP_REPLACE(LOWER({column}), '[^a-z0-9]+', '-'))"
