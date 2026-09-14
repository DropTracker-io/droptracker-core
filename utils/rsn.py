"""OSRS player-name equivalence -- the one rule for matching a player name.

The game treats '-', '_' and ' ' as one character in a display name: the
hiscores return the same account for "1-19", "1 19" and "1_19", and two
accounts can never differ only by them. Wise Old Man folds all three to a
space, which is usually the spelling ``players.player_name`` stores, while the
plugin and people type the game's spelling. ``utf8mb4_general_ci`` bridges
case but not separators, and ``ilike`` reads '_' as a wildcard.

Deliberately free of ``db`` and Discord imports: ``utils.format`` imports
``db``, whose package init imports ``db.ops``, which imports ``utils.embeds``,
which imports ``utils.format`` -- so the first ``utils.format`` import in a
process that has not already loaded ``db`` fails on the cycle. The Data API is
such a process (2026-09-13: every ``/v2/players/<ref>`` returned 500 for 91s).
``utils.format`` re-exports everything here for existing importers.
"""
import re
import unicodedata

from sqlalchemy import String, func


def normalize_player_display_equivalence(name: str) -> str:
    """
    Normalize a player name for equivalence comparison where the external
    library replaces hyphens/underscores with spaces. This keeps alphanumerics
    and converts '-', '_' to a single space, then collapses whitespace and
    lowercases for robust comparison.
    """
    if name is None:
        return ""
    # Replace '-' and '_' with spaces, collapse whitespace, and lowercase
    name = str(name).replace('-', ' ').replace('_', ' ')
    name = " ".join(name.split())
    return name.lower()


def normalize_claim_rsn_input(name: str) -> str:
    """
    Normalize an RSN from Discord or other UI before DB lookup: NFKC, common
    unicode space characters to ASCII space, collapse runs of whitespace, strip.
    """
    if name is None:
        return ""
    s = unicodedata.normalize("NFKC", str(name).strip())
    s = re.sub(r"[\u00a0\u2000-\u200b\u202f\u205f\u3000]", " ", s)
    s = " ".join(s.split())
    return s


def find_player_by_rsn(sess, player_model, rsn: str):
    """
    Resolve the Player someone means by an RSN they typed (claims, the points
    commands, API lookups): the exact name first, then OSRS name equivalence
    through the indexed ``player_name_norm`` column.

    The game treats '-', '_' and ' ' as one character -- the hiscores return
    the same account for "1-19", "1 19" and "1_19" -- and WOM folds all three
    to a space, which is usually the spelling we store. An exact or ``ilike``
    match therefore misses every hyphenated RSN typed the way the game shows
    it: "1-19" could not claim its own "1 19" row (ticket #434), nor
    "Tzuk-Kal-Lag" find "tzuk kal lag". ``ilike`` also reads '_' as a wildcard.

    The exact step is a plain ``=``: ``players.player_name`` is
    ``utf8mb4_general_ci``, already case-insensitive, and an indexed seek
    (~0.4ms) where ``lower(trim(...))`` scanned every row (~15ms).

    Ties go to the lowest player_id in both steps, the same rule as
    db.ops.resolve_player_for_display: it prefers the original account over a
    later wom_temp stub with the same name ("Brondt" / "brondt").
    """
    norm = normalize_claim_rsn_input(rsn)
    if not norm:
        return None
    p = (
        sess.query(player_model)
        .filter(player_model.player_name == norm)
        .order_by(player_model.player_id)
        .first()
    )
    if p:
        return p
    folded = normalize_player_display_equivalence(norm)
    if not folded:
        return None
    return (
        sess.query(player_model)
        .filter(player_model.player_name_norm == folded)
        .order_by(player_model.player_id)
        .first()
    )


def pick_player_by_rsn(players, rsn: str):
    """``find_player_by_rsn`` over rows already in hand -- a user's own
    accounts, a group roster -- with the same exact-then-equivalent order and
    lowest-player_id tie-break."""
    norm = normalize_claim_rsn_input(rsn)
    if not norm:
        return None
    ordered = sorted((p for p in players if p is not None),
                     key=lambda p: p.player_id or 0)
    wanted = norm.lower()
    for p in ordered:
        if (p.player_name or "").strip().lower() == wanted:
            return p
    folded = normalize_player_display_equivalence(norm)
    if not folded:
        return None
    for p in ordered:
        if normalize_player_display_equivalence(p.player_name) == folded:
            return p
    return None


def rsn_contains(player_name: str, text: str) -> bool:
    """Whether typed filter/autocomplete ``text`` occurs in ``player_name``
    under OSRS name equivalence, so "tzuk-kal" finds "tzuk kal lag". Text that
    folds to nothing (blank, or only separators) filters nothing out."""
    needle = normalize_player_display_equivalence(text)
    return not needle or needle in normalize_player_display_equivalence(player_name)


def player_name_search_expr(column):
    """SQL fold of a player-name column for substring search: match it with
    ``.contains(normalize_player_display_equivalence(text), autoescape=True)``.

    Deliberately not ``player_name_norm LIKE '%...%'``: that generated column is
    only cheap as an index seek. In a scan MariaDB re-evaluates its
    REGEXP_REPLACE per row -- 110ms against 11ms for the plain column, where
    these two REPLACEs cost ~15ms. Whitespace runs are not collapsed here, but
    OSRS names cannot contain them.
    """
    return func.replace(
        func.replace(func.lower(column), "-", " "), "_", " ", type_=String
    )
