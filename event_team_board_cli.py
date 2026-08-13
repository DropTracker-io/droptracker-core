"""Manual driver for per-team event lootboards (dev-tracker t63).

Mirrors ``board_cli.py`` (the per-group equivalent). Always ``force``s, which
ignores the hourly mtime throttle — this is the "generate it now so I can look
at it" tool. It does NOT bypass the ``EVENT_TEAM_LOOTBOARDS`` flag or the
public-visibility gate: these images land on an unauthenticated ``/img`` URL,
so switching the feature on is the operator's explicit decision, not a
side effect of running a CLI.

    EVENT_TEAM_LOOTBOARDS=1 ./venv/bin/python event_team_board_cli.py --event 42
    EVENT_TEAM_LOOTBOARDS=1 ./venv/bin/python event_team_board_cli.py --team 7
    EVENT_TEAM_LOOTBOARDS=1 ./venv/bin/python event_team_board_cli.py --all --limit 5
"""
import argparse
import asyncio
from typing import Optional


async def _run(event_id: Optional[int], team_id: Optional[int],
               limit: int) -> None:
    from lootboard import team_boards

    if not team_boards.feature_enabled():
        print(f"{team_boards.FEATURE_FLAG_ENV} is off — nothing generated. "
              f"Re-run with {team_boards.FEATURE_FLAG_ENV}=1 to render.")
        return

    if team_id is not None:
        from db.models import Event, EventTeam, Session

        with Session() as session:
            team = (session.query(EventTeam)
                    .filter(EventTeam.id == team_id).first())
            if team is None:
                print(f"Team {team_id} not found")
                return
            event = (session.query(Event)
                     .filter(Event.id == team.event_id).first())
            if event is None:
                print(f"Event {team.event_id} not found")
                return
            if event_id is not None and int(event.id) != int(event_id):
                print(f"Team {team_id} belongs to event {event.id}, "
                      f"not {event_id}")
                return
            path = await team_boards.render_team_board(
                session, event, team, force=True)
        print(path or "Nothing generated (private event, empty roster, or "
                      "the event has not started).")
        return

    written = await team_boards.sweep_team_boards(
        event_id=event_id, force=True, limit=limit)
    print(f"Generated {len(written)} team board(s)")
    for path in written:
        print(f"  {path}")


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate per-team event lootboards."
    )
    parser.add_argument("--event", type=int, default=None,
                        help="Event ID to generate boards for.")
    parser.add_argument("--team", type=int, default=None,
                        help="Single team ID (implies its event).")
    parser.add_argument("--all", action="store_true",
                        help="Every team of every active event.")
    parser.add_argument("--limit", type=int,
                        default=None,
                        help="Max boards per run (default: the driver's cap).")
    args = parser.parse_args(argv)

    if args.team is None and args.event is None and not args.all:
        parser.error("pass --event, --team or --all")

    from lootboard.team_boards import TEAM_BOARDS_PER_RUN

    limit = TEAM_BOARDS_PER_RUN if args.limit is None else args.limit
    asyncio.run(_run(args.event, args.team, limit))


if __name__ == "__main__":
    main()
