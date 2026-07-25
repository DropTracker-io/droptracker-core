"""One-off: re-pend live team-channel rows so the core bot renames them onto
the colored-icon scheme ("team-blue" -> "🔵┃blue-team", "Blue Team" ->
"🔵┃Blue Team" for forum threads).

Channels are only ever (re)named while a ``web_event_team_discord`` row is
``pending``, and the Web API re-pends solely on a config/team mutation — so
without this, existing events would keep their old names until someone edited
them. Flipping ``synced``/``failed`` rows back to ``pending`` makes the
reconciler re-run ``_ensure_channel`` on its next ~30s tick; the name is the
only thing that differs, so everything else is a no-op.

Idempotent (a second run finds the rows already renamed and re-pends them to
the same names). Rows of ``past`` events are skipped — they are on their way
out and a rename would waste a Discord call. Run with the core bot up:

    venv/bin/python -m scripts.repend_event_team_channels           # dry run
    venv/bin/python -m scripts.repend_event_team_channels --apply
"""
from __future__ import annotations

import argparse

from db.models import Event, EventTeam, EventTeamDiscord, Session
from services.event_team_discord import (
    channel_name_for_team,
    team_icon_index,
    thread_name_for_team,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default: dry run)")
    args = parser.parse_args()

    session = Session()
    try:
        rows = (session.query(EventTeamDiscord, EventTeam, Event)
                .join(EventTeam, EventTeam.id == EventTeamDiscord.team_id)
                .join(Event, Event.id == EventTeamDiscord.event_id)
                .filter(EventTeamDiscord.channel_id.isnot(None),
                        EventTeamDiscord.sync_status.in_(("synced", "failed")),
                        Event.status != "past")
                .order_by(EventTeamDiscord.id.asc())
                .all())
        for row, team, event in rows:
            index = team_icon_index(session, event.id, team.id)
            namer = (thread_name_for_team if row.channel_kind == "thread"
                     else channel_name_for_team)
            name = namer(team.name, team.color, index)
            print(f"event {event.id} ({event.name!r}) team {team.id} "
                  f"({team.name!r}, color={team.color}) "
                  f"{row.channel_kind} {row.channel_id} -> {name}")
            row.sync_status = "pending"
            row.last_error = None
        if args.apply:
            session.commit()
            print(f"Done — {len(rows)} row(s) re-pended; the core bot renames "
                  f"them on its next ~30s reconcile tick.")
        else:
            session.rollback()
            print(f"Dry run — {len(rows)} row(s) would be re-pended. "
                  f"Re-run with --apply to write.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
