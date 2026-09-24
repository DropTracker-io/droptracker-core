"""Delete one event team and every row scoped to it.

The single cascade behind every way a team disappears: an admin deleting it
(``DELETE /events/{id}/teams/{tid}``), a clan withdrawing from or being removed
from a staff-hosted clan-vs-clan event, and the start-of-event drop of a clan
whose roster is under the minimum (web119a). One implementation, because the
cascade has already drifted once: P0-5 found five newer child tables the old
four-table version missed, which made pointed and board-game teams
undeletable.

No ORM cascade is configured on these FKs, so children go first. Deleting the
team's ledger and progress is correct: standings recompute from the remaining
teams. Buy-ins are the exception: that GP was contributed to the *event* and
may already be paid, so the rows go back to the unassigned bucket instead.

Module-level imports are stdlib-only (lazy db/service imports inside), so the
event worker and the unit tests can load it without the web layer.
"""
from __future__ import annotations


def _drop_team_discord_rows(session, event_id: int, team_id: int) -> None:
    """Queue live role/channel teardown for the bot, then drop the rows (their
    team FK is about to go away). Teardown loss is tolerable; a dangling row
    is not."""
    try:
        from db import EventTeamDiscord
        from services.event_team_discord import (
            enqueue_team_discord_orphans,
            orphan_team_discord_payloads,
        )
    except ImportError:
        return
    try:
        from utils.redis import redis_client

        enqueue_team_discord_orphans(
            redis_client, orphan_team_discord_payloads(session, event_id, team_id))
    except Exception:
        pass
    session.query(EventTeamDiscord).filter(
        EventTeamDiscord.team_id == team_id
    ).delete(synchronize_session=False)


def purge_team(session, event_id: int, team) -> None:
    """Delete ``team`` (an ``EventTeam`` of ``event_id``) and its children.
    Flushes; the caller owns the commit, the audit row and any follow-up
    Discord re-sync of the surviving teams."""
    from sqlalchemy import or_

    from db import (
        EventBingoCompletion,
        EventBoardEffect,
        EventBoardPosition,
        EventCoinLedger,
        EventCompletion,
        EventLeaderVote,
        EventPlayerPoints,
        EventProgress,
        EventTeamCooldown,
        EventTeamInventory,
        EventTeamMember,
    )
    from services.event_buyins import release_team_buyins

    team_id = team.id
    _drop_team_discord_rows(session, event_id, team_id)

    for model in (EventBingoCompletion, EventCompletion, EventProgress, EventTeamMember):
        session.query(model).filter(model.team_id == team_id).delete(
            synchronize_session=False)

    release_team_buyins(session, event_id, team_id)

    # P0-5: points/vote + board-game children (web45a–web48a). Several are
    # NOT NULL (board position, inventory, cooldown, coin ledger).
    for model in (EventPlayerPoints, EventLeaderVote, EventBoardPosition,
                  EventTeamInventory, EventTeamCooldown, EventCoinLedger):
        session.query(model).filter(model.team_id == team_id).delete(
            synchronize_session=False)
    # Effects reference a team from either side (source_team_id NOT NULL).
    session.query(EventBoardEffect).filter(
        or_(EventBoardEffect.source_team_id == team_id,
            EventBoardEffect.target_team_id == team_id)
    ).delete(synchronize_session=False)
    session.delete(team)
    session.flush()
