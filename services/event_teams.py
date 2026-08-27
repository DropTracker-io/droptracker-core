"""Team cosmetics that surfaces outside Discord and the website need (web103a).

Today that means the in-game clan-chat badge: the plugin prints a short tag
and/or a colored orb beside a clanmate's name so you can read the room during a
bingo or a clan-vs-clan without cross-referencing the site.

Two rules hold this together:

* **The tag is derived, not stored, until an admin overrides it.**
  ``EventTeam.short_tag`` NULL means "derive from the name", so a team has a
  badge the moment it exists and a rename re-derives instead of silently
  keeping a stale tag. :func:`derive_short_tag` is pure — same name in, same
  tag out, forever — which is what lets the client cache a roster.
* **The orb is the one Discord already shows.** Team channels are named
  "🔵┃blue-team" (services.event_team_discord), so the in-game badge resolves
  through the same hue bands and comes back with that circle's own fill color.
  Chat, the Discord channel list and the site's team dot then agree by
  construction rather than by three palettes that drift.

Module-level imports are stdlib-only (the unit tests load this file directly,
the same convention as services/event_team_discord.py); everything else is
lazy-imported inside functions.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

# Longest tag the column stores and the client is willing to print. Eight
# characters is already wide in a chat line — the derivation targets 2-4.
SHORT_TAG_MAX = 8

# What an admin may type. The game's chat font has no glyph for most of
# Unicode, so a tag outside this set would render as blank boxes in the very
# place it exists to be read.
SHORT_TAG_ALLOWED = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "

# How many leading characters a single-word name contributes ("Vanguard" ->
# "VAN"), and the initials cap for a multi-word one ("The Sunday Night
# Regulars" -> "TSNR").
_SINGLE_WORD_CHARS = 3
_MAX_INITIALS = 4


# Punctuation that sits INSIDE a word rather than between two. Dropped before
# splitting so "Zezima's Crew" initials as ZC, not ZSC — a hyphen, by contrast,
# really does separate words in a team name ("Red-Rockets").
_INTRA_WORD = "'’`"


def _words(name: str) -> List[str]:
    """Alphanumeric words in a team name, hyphens/underscores counting as
    breaks ("Red-Rockets" is two words, not one)."""
    out: List[str] = []
    current: List[str] = []
    for ch in str(name or ""):
        if ch in _INTRA_WORD:
            continue
        if ch.isascii() and ch.isalnum():
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def derive_short_tag(name) -> str:
    """The chat tag for a team that has not set one. Pure and total.

    Multi-word names give their initials (capped at :data:`_MAX_INITIALS`);
    a single word gives its first :data:`_SINGLE_WORD_CHARS` characters. A name
    with nothing usable in it — empty, or entirely non-ASCII — still has to
    produce something printable, so it falls back to "T"; :func:`assign_short_tags`
    then keeps sibling teams apart.
    """
    words = _words(name)
    if not words:
        return "T"
    if len(words) == 1:
        return words[0][:_SINGLE_WORD_CHARS].upper()
    return "".join(word[0] for word in words[:_MAX_INITIALS]).upper()


def assign_short_tags(teams: Iterable) -> dict:
    """``team_id -> tag`` for one event, admin overrides winning and
    collisions broken.

    Two teams whose names derive to the same tag ("Red Ravens" and "Red
    Rockets" are both RR) would print an identical badge, which is worse than
    no badge — the reader draws a confident wrong conclusion. The second and
    later claimants get a counter appended.

    Deterministic in the iteration order given: pass teams in id order (their
    creation order, stable across renames, recolors and roster edits) so the
    same event always renders the same tags.
    """
    assigned: dict = {}
    taken = set()
    for team in teams:
        raw = getattr(team, "short_tag", None)
        tag = sanitize_short_tag(raw) or derive_short_tag(getattr(team, "name", None))
        candidate = tag
        suffix = 2
        while candidate.lower() in taken:
            room = SHORT_TAG_MAX - len(str(suffix))
            candidate = f"{tag[:room]}{suffix}"
            suffix += 1
        taken.add(candidate.lower())
        assigned[getattr(team, "id", None)] = candidate
    return assigned


def sanitize_short_tag(raw) -> Optional[str]:
    """An admin-supplied tag reduced to what chat can print, or None.

    Returns None for anything that survives as empty, which is the same signal
    as an unset column: derive one instead.
    """
    if raw is None:
        return None
    cleaned = "".join(ch for ch in str(raw) if ch in SHORT_TAG_ALLOWED).strip()
    cleaned = " ".join(cleaned.split())
    return cleaned[:SHORT_TAG_MAX] or None


def team_badge(team, index: int = 0) -> dict:
    """The badge fields the plugin renders for one team.

    ``orb``/``orb_color`` are the circle Discord shows for this team's channel
    and that circle's own fill: the client draws a chat sprite in ``orb_color``
    so the in-game badge matches the Discord channel icon, rather than the raw
    accent color, which sits somewhere between two orbs.
    """
    from services.event_team_discord import team_orb

    orb, orb_color = team_orb(getattr(team, "color", None), index)
    return {
        "orb": orb,
        "orb_color": orb_color,
        "color": getattr(team, "color", None),
        "icon_item_id": getattr(team, "piece_item_id", None),
    }
