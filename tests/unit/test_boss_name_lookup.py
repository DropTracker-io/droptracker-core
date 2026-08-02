"""Regression tests for utils.format._lookup_boss_name — the NPC resolution the
adventure-log processor uses to turn a PB line's boss label into an npc_id.

Two ways it used to land PBs on the wrong npc_list row (both observed live in
2026-08, splitting every Gauntlet PB board in two):

  1. The exact-name lookup ran first, so "Corrupted Hunllef" (the name the
     plugin's chat-PB path reports) matched its own npc row instead of the
     activity row the loot path uses, "The Corrupted Gauntlet".
  2. The last-resort ``ilike("%name%")`` matched any row merely MENTIONING the
     boss — "Wintertodt" resolved to "Reward cart (Wintertodt)".
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from utils import format as fmt


class _Column:
    """Stands in for NpcList.npc_name, recording how it was compared."""

    def __init__(self):
        self.equals = []
        self.ilikes = []

    def __eq__(self, other):
        self.equals.append(other)
        return ("eq", other)

    def ilike(self, pattern):
        self.ilikes.append(pattern)
        return ("ilike", pattern)

    __hash__ = None


@pytest.fixture
def npc_name_column(monkeypatch):
    column = _Column()
    monkeypatch.setattr(fmt, "NpcList", SimpleNamespace(npc_name=column))
    return column


def _session(query_results=(), execute_row=None):
    """Session whose ORM .first() calls return `query_results` in order and
    whose raw normalized-match execute() returns `execute_row`."""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.side_effect = list(query_results)
    session.execute.return_value.first.return_value = execute_row
    return session


def test_encounter_member_resolves_before_the_exact_name_lookup(npc_name_column):
    session = _session(query_results=[SimpleNamespace(npc_name="The Corrupted Gauntlet",
                                                      npc_id=13942)])

    assert fmt._lookup_boss_name(session, "Corrupted Hunllef") == ("The Corrupted Gauntlet", 13942)
    # The name was rewritten BEFORE the query — never looked up as "Corrupted
    # Hunllef", whose own npc_list row (9035) is what split the PB board.
    assert npc_name_column.equals == ["The Corrupted Gauntlet"]


def test_normalized_match_prefers_the_canonical_spelling(npc_name_column):
    session = _session(query_results=[None], execute_row=(13703, "The Gauntlet", 1))

    # "Gauntlet" is the adventure log's short form: no exact row, so it falls
    # through to the variant match.
    assert fmt._lookup_boss_name(session, "Gauntlet") == ("The Gauntlet", 13703)
    sql = str(session.execute.call_args[0][0])
    params = session.execute.call_args[0][1]
    # Canonical spellings outrank alias spellings, and that term outranks the
    # "has tracked data" tie-break — three stray drops on Crystalline Hunllef
    # were otherwise enough to win it the whole board.
    assert sql.index("primary_variants") < sql.index("tracked DESC")
    assert params["primary_variants"] == ["gauntlet", "the-gauntlet"]
    assert "crystalline-hunllef" in params["variants"]


def test_close_match_is_anchored_to_the_start_of_the_name(npc_name_column):
    session = _session(query_results=[None, None])

    assert fmt._lookup_boss_name(session, "Wintertodt") == ("Unknown", None)
    # Not "%Wintertodt%": that matched "Reward cart (Wintertodt)".
    assert npc_name_column.ilikes == ["Wintertodt%"]


def test_close_match_still_finds_a_more_specific_row(npc_name_column):
    session = _session(query_results=[None, SimpleNamespace(npc_name="Doom of Mokhaiotl (Level 3)",
                                                            npc_id=14710)])

    assert fmt._lookup_boss_name(session, "Doom of Mokhaiotl") == (
        "Doom of Mokhaiotl (Level 3)", 14710,
    )
    assert npc_name_column.ilikes == ["Doom of Mokhaiotl%"]
