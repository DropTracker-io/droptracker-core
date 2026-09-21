"""Re-pend live team Discord rows so the core bot re-derives their names and
colors: the channel circle ("🔵┃blue-team", "🔵┃Blue Team" for threads and
voice channels) and the team role's color.

Channels and roles are only ever (re)named or recolored while a
``web_event_team_discord`` row is ``pending``, and the Web API re-pends solely
on a config/team mutation, so a change to how names or colors are DERIVED
never reaches existing events until someone edits them. Run this after such a
change. History:

* colored-icon scheme ("team-blue" -> "🔵┃blue-team");
* 2026-09: colorless teams take the site's palette default (red, blue, green,
  ...) instead of a separate Discord-only emoji rotation, and roles always
  carry the team's effective color.

Flipping ``synced``/``failed`` rows back to ``pending`` makes the reconciler
re-run ``_ensure_role``/``_ensure_channel``/``_ensure_voice_channel`` on its
next ~30s tick; anything already right is a no-op there.

Idempotent (a second run finds the rows already right and re-pends them to
the same names and colors). Rows of ``past`` events are skipped: they are on
their way out and a rename would waste a Discord call. Run with the core bot
up:

    venv/bin/python -m scripts.repend_event_team_channels           # dry run
    venv/bin/python -m scripts.repend_event_team_channels --apply
"""
from __future__ import annotations

import argparse

from sqlalchemy import or_

from db.models import Event, EventTeam, EventTeamDiscord, Session
from services.event_team_discord import (
    channel_name_for_team,
    effective_team_color,
    team_icon_index,
    thread_name_for_team,
    voice_name_for_team,
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
                .filter(or_(EventTeamDiscord.channel_id.isnot(None),
                            EventTeamDiscord.role_id.isnot(None),
                            EventTeamDiscord.voice_channel_id.isnot(None)),
                        EventTeamDiscord.sync_status.in_(("synced", "failed")),
                        Event.status != "past")
                .order_by(EventTeamDiscord.id.asc())
                .all())
        for row, team, event in rows:
            index = team_icon_index(session, event.id, team.id)
            targets = []
            if row.role_id:
                targets.append(f"role {row.role_id} -> "
                               f"{effective_team_color(team.color, index)}")
            if row.channel_id:
                namer = (thread_name_for_team if row.channel_kind == "thread"
                         else channel_name_for_team)
                targets.append(f"{row.channel_kind} {row.channel_id} -> "
                               f"{namer(team.name, team.color, index)}")
            if row.voice_channel_id:
                targets.append(f"voice {row.voice_channel_id} -> "
                               f"{voice_name_for_team(team.name, team.color, index)}")
            print(f"event {event.id} ({event.name!r}) team {team.id} "
                  f"({team.name!r}, color={team.color}): " + "; ".join(targets))
            row.sync_status = "pending"
            row.last_error = None
        if args.apply:
            session.commit()
            print(f"Done — {len(rows)} row(s) re-pended; the core bot updates "
                  f"them on its next ~30s reconcile tick.")
        else:
            session.rollback()
            print(f"Dry run — {len(rows)} row(s) would be re-pended. "
                  f"Re-run with --apply to write.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
