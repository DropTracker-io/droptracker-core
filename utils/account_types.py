"""OSRS account types (game modes), as reported by the RuneLite plugin.

Two independent paths carry a player's game mode, and both decode the same
in-game value (varbit 1777 / ``VarbitID.IRONMAN``):

* the **state-sync snapshot** (``POST /state/sync``) sends the raw varbit as an
  int, stored on ``player_state.account_type``;
* **submissions** may carry the already-decoded wire string (Task 23), stored
  on ``players.account_type``.

Keeping the enum here means the two cannot drift, and a new game mode is a
one-line change rather than a hunt through both pipelines.
"""
from __future__ import annotations

from typing import Optional

# The tuple index IS the varbit value — this ordering is the wire contract,
# matched by the frontend's AccountTypeSchema. Append only; never reorder.
ACCOUNT_TYPES_BY_VARBIT = (
    "normal",
    "ironman",
    "ultimate_ironman",
    "hardcore_ironman",
    "group_ironman",
    "hardcore_group_ironman",
    "unranked_group_ironman",
)

VALID_ACCOUNT_TYPES = frozenset(ACCOUNT_TYPES_BY_VARBIT)


def account_type_from_varbit(value) -> Optional[str]:
    """Decode a raw varbit 1777 value into its wire string.

    Returns ``None`` for anything unrecognized — absent, non-integer, or a
    mode this build predates — so a future game mode degrades to "no badge"
    instead of raising on a profile load.
    """
    # bool is an int subclass, and True would otherwise decode as "ironman".
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 <= value < len(ACCOUNT_TYPES_BY_VARBIT):
        return ACCOUNT_TYPES_BY_VARBIT[value]
    return None
