"""Prize-pot ↔ roster coupling: keep every buy-in pointed at its payer's
current team (web71a).

Buy-ins are collected at **sign-up**, which for a drafted event happens long
before anyone knows which team a player will end up on. ``EventBuyin.team_id``
is nullable for exactly that reason, so an unassigned participant's buy-in is a
first-class row — no placeholder team required to record that someone paid.

The one invariant that makes it work lives here:

    a live buy-in row's ``team_id`` always mirrors where its payer currently
    sits on the event's roster — a real team, or NULL while they are still in
    the sign-up pool.

Every roster mutation (self-join, admin add/move/remove, pool
assign/unassign/randomize, auto-clan reconcile, team delete) calls in. When the
draft lands, buy-ins an admin already ticked *paid* follow their players onto
the new teams: per-team pot totals and the roster checklist stay correct and
nothing is re-entered.

Two deliberate exclusions:

* only ``kind='buyin'`` rows move — a **donation** is standalone GP credited to
  a team (or to nobody) on purpose, not something derived from the roster;
* ``status='void'`` rows never move — they are history.

Lives in ``services/`` rather than beside :mod:`web_api.event_prizes` because
both layers place players: the web routes *and* the bot-shared sign-up /
lifecycle services. Module imports stay stdlib-only (db imports are
function-local) so it loads under the unit-test conftest. Callers own the
commit.
"""
from __future__ import annotations

from typing import Mapping, Optional


def sync_buyin_teams(session, event_id: int, placements: Mapping[int, Optional[int]]) -> int:
    """Re-point each player's live buy-ins on this event at their new team.

    ``placements`` maps ``player_id -> team_id`` (``None`` = back in the pool /
    off the roster). Returns the number of ledger rows touched. Players with no
    buy-in row are simply a no-op, so callers never need to check first.
    """
    from db.models import EventBuyin

    if not placements:
        return 0

    # One UPDATE per distinct destination team, not per player: a randomized
    # draft places the whole pool at once and lands on a handful of teams.
    by_team: dict = {}
    for player_id, team_id in placements.items():
        by_team.setdefault(team_id, []).append(player_id)

    moved = 0
    for team_id, player_ids in by_team.items():
        moved += (
            session.query(EventBuyin)
            .filter(
                EventBuyin.event_id == event_id,
                EventBuyin.player_id.in_(player_ids),
                EventBuyin.kind == "buyin",
                EventBuyin.status != "void",
            )
            .update({EventBuyin.team_id: team_id}, synchronize_session=False)
        )
    return moved


def sync_buyin_team(session, event_id: int, player_id: int, team_id: Optional[int]) -> int:
    """:func:`sync_buyin_teams` for a single placement."""
    return sync_buyin_teams(session, event_id, {player_id: team_id})


def release_team_buyins(session, event_id: int, team_id: int) -> int:
    """Return a deleted team's live buy-ins to the pool (``team_id -> NULL``).

    A team can be deleted while its members' buy-ins are already paid; the GP
    was contributed to the *event*, so the rows outlive the team rather than
    being wiped with it. Unlike :func:`sync_buyin_teams` this releases **every**
    kind and status, voided rows included — their ``team_id`` FK points at a
    row that is about to disappear, and the constraint would block the delete.
    """
    from db.models import EventBuyin

    return (
        session.query(EventBuyin)
        .filter(
            EventBuyin.event_id == event_id,
            EventBuyin.team_id == team_id,
        )
        .update({EventBuyin.team_id: None}, synchronize_session=False)
    )
