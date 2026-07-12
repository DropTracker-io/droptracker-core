"""One-off: re-sync web_event_guilds for existing DRAFT events after the
web35a migration (discord_event_policy gate).

Existing drafts created before the gate shipped may still have live
web_event_guilds rows (and real Discord scheduled events — e.g. the
2026-07-11 duplicate "Nightmare in Vampyrium" pair, events 6/7). Because the
Web API only re-syncs on mutation, those rows would linger until someone
edits the event. This script runs sync_event_guilds() once per draft: with
the default 'on_activate' policy the desired set is empty, so every row is
marked delete_pending and the core bot's reconciler deletes the actual
Discord scheduled events within ~30s.

Safe to re-run (idempotent desired-state sync). Run AFTER `alembic upgrade
head` and with the core bot running (it performs the Discord-side deletes):

    venv/bin/python -m scripts.resync_draft_event_guilds
"""
from __future__ import annotations

from db.models import Event, Session
from services.event_scheduled_events import sync_event_guilds


def main() -> None:
    session = Session()
    try:
        drafts = session.query(Event).filter(Event.status == "draft").all()
        for ev in drafts:
            sync_event_guilds(session, ev)
            print(f"re-synced draft event {ev.id} ({ev.name!r}, policy="
                  f"{getattr(ev, 'discord_event_policy', 'on_activate')})")
        session.commit()
        print(f"Done — {len(drafts)} draft event(s) re-synced. The bot "
              f"reconciler retires any now-undesired scheduled events on its "
              f"next ~30s tick.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
