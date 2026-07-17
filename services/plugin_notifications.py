"""Event → plugin in-game notification inbox (P0 of the plan in
docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md).

Per-player Redis inbox (``plugin:notify:{player_id}``) drained by
``GET /notifications`` on the intake API. Producers:

- ``services/event_engine._enqueue_notification`` → :func:`fan_out_event_notification`
  (called BEFORE the Discord message_config mute gate — a player's in-game
  notifications are independent of the event's Discord verbosity config),
- ``workers/webhook_consumer`` → :func:`push_submission_notice` (restores the
  ``/webhook`` response ``notice`` channel that queue mode silenced).

Safety contract: inbox entries are typed data only —
``{id, type, ts, event?, data}``. The server never sends display strings or
markup for event types; the plugin owns rendering via a hardcoded
type→renderer registry and silently drops unknown types. The one exception is
``submission_notice`` (legacy processor notice text), which the plugin renders
as sanitized plain chat gated by its existing receiveInGameMessages toggle.

Per-type website prefs (``player_notification_prefs``) are enforced here at
delivery time: a disabled type never enters the inbox. ``event_task_progress``
is deliberately NOT web-configurable — it is always delivered and filtered by
a plugin-side config toggle (the mute switch for the noisiest type lives in
game, and no option may exist in two places).

Module-level imports are stdlib-only on purpose (the unit tests load this file
directly and the conftest stubs ``db``/``utils``); anything DB- or
Redis-shaped is lazy-imported inside functions. All Redis delivery is
best-effort: a Redis outage must never break event processing or submission
intake.
"""
from __future__ import annotations

import json
import time
import uuid

INBOX_KEY_TEMPLATE = "plugin:notify:{player_id}"
INBOX_CAP = 50
INBOX_TTL_SECONDS = 24 * 3600
DRAIN_BATCH_LIMIT = 25
# Free-form notice text is capped defensively; the plugin caps again on render.
NOTICE_MAX_CHARS = 500

AUDIENCE_TEAM = "team"
AUDIENCE_EVENT = "event"

# Which notification_queue event types are delivered in-game, and to whom.
# Types absent here (event_pending, event_activation_failed,
# event_signup_prompt, event_pot) are Discord admin/announcement concerns
# with no in-game audience.
AUDIENCE_FOR_TYPE = {
    "event_completion": AUDIENCE_TEAM,
    "event_task_progress": AUDIENCE_TEAM,
    "event_line": AUDIENCE_TEAM,
    "event_blackout": AUDIENCE_TEAM,
    "event_board_turn": AUDIENCE_TEAM,
    "event_board_roll_prompt": AUDIENCE_TEAM,
    "event_lead_change": AUDIENCE_EVENT,
    "event_started": AUDIENCE_EVENT,
    "event_ended": AUDIENCE_EVENT,
}

# Types a player may switch off on the website (P1 UI writes
# player_notification_prefs). Absent-from-prefs means enabled — defaults are
# all-on. event_task_progress is client-toggle-only by design (see module
# docstring); submission_notice is gated by the plugin's existing
# receiveInGameMessages config.
WEB_PREF_TYPES = (
    "event_completion",
    "event_line",
    "event_blackout",
    "event_board_turn",
    "event_board_roll_prompt",
    "event_lead_change",
    "event_started",
    "event_ended",
)


def _redis():
    """Raw redis handle (the RedisClient wrapper exposes no pipeline/list-trim ops)."""
    from utils.redis import RedisClient

    return RedisClient().client


def _inbox_key(player_id) -> str:
    return INBOX_KEY_TEMPLATE.format(player_id=int(player_id))


def build_envelope(notification_type: str, data: dict, event: dict = None,
                   now: int = None) -> dict:
    """The typed wire envelope. Versionless by design: future needs are new
    ``type`` strings (which unaware plugins drop), never mutations of existing
    ones."""
    ts = int(now if now is not None else time.time())
    envelope = {
        "id": f"{ts}-{uuid.uuid4().hex[:8]}",
        "type": notification_type,
        "ts": ts,
        "data": dict(data or {}),
    }
    if isinstance(event, dict) and event.get("id") is not None:
        envelope["event"] = {"id": event.get("id"), "name": event.get("name")}
    return envelope


def push_to_inbox(player_id, envelope: dict) -> bool:
    """Best-effort append to one player's inbox (capped, TTL-refreshed)."""
    if not player_id:
        return False
    try:
        serialized = json.dumps(envelope, default=str)
        key = _inbox_key(player_id)
        pipe = _redis().pipeline()
        pipe.rpush(key, serialized)
        pipe.ltrim(key, -INBOX_CAP, -1)
        pipe.expire(key, INBOX_TTL_SECONDS)
        pipe.execute()
        return True
    except Exception as e:
        print(f"[plugin_notifications] inbox push failed for player {player_id}: {e}")
        return False


def drain_inbox(player_id, limit: int = DRAIN_BATCH_LIMIT) -> list:
    """Pop up to ``limit`` entries (FIFO). LRANGE+LTRIM run in one MULTI/EXEC
    pipeline; the only reader of a given inbox is that player's own plugin, so
    there is no competing consumer to race."""
    if not player_id:
        return []
    key = _inbox_key(player_id)
    try:
        pipe = _redis().pipeline()
        pipe.lrange(key, 0, int(limit) - 1)
        pipe.ltrim(key, int(limit), -1)
        raw_items = pipe.execute()[0] or []
    except Exception as e:
        print(f"[plugin_notifications] inbox drain failed for player {player_id}: {e}")
        return []
    entries = []
    for item in raw_items:
        try:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            entries.append(json.loads(item))
        except Exception:
            continue
    return entries


def push_submission_notice(player_id, message) -> bool:
    """Deliver a processor notice (the pre-queue-mode /webhook ``notice``) to
    the submitting player's inbox."""
    if not player_id or not message:
        return False
    data = {"message": str(message)[:NOTICE_MAX_CHARS]}
    return push_to_inbox(player_id, build_envelope("submission_notice", data))


def player_has_active_event(session, player_id) -> bool:
    """True when the player is on a team roster of a live event — the plugin's
    signal to start polling GET /notifications (analogue of track_xp_events)."""
    from sqlalchemy import text

    row = session.execute(
        text(
            "SELECT 1 FROM web_event_team_members m "
            "JOIN web_event_teams t ON t.id = m.team_id "
            "JOIN web_events e ON e.id = t.event_id "
            "WHERE m.player_id = :player_id AND e.status = 'active' LIMIT 1"
        ),
        {"player_id": int(player_id)},
    ).first()
    return row is not None


def _team_player_ids(session, team_id) -> list:
    from db.models.events import EventTeamMember

    rows = (
        session.query(EventTeamMember.player_id)
        .filter(EventTeamMember.team_id == int(team_id))
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def _event_player_ids(session, event_id) -> list:
    from db.models.events import EventTeam, EventTeamMember

    rows = (
        session.query(EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == int(event_id))
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def _players_with_type_disabled(session, notification_type: str, player_ids) -> set:
    """Subset of player_ids whose stored website prefs disable this type.
    Missing rows / missing keys mean enabled (defaults are all-on)."""
    if notification_type not in WEB_PREF_TYPES or not player_ids:
        return set()
    # Fail-open: a prefs lookup failure (e.g. table not migrated yet) must
    # not kill delivery — defaults are all-on anyway.
    try:
        from db.models import PlayerNotificationPrefs

        rows = (
            session.query(PlayerNotificationPrefs)
            .filter(PlayerNotificationPrefs.player_id.in_(list(player_ids)))
            .all()
        )
    except Exception as e:
        print(f"[plugin_notifications] prefs lookup failed (delivering with defaults): {e}")
        return set()

    disabled = set()
    for row in rows:
        try:
            prefs = json.loads(row.prefs or "{}")
        except Exception:
            continue
        if prefs.get(notification_type) is False:
            disabled.add(row.player_id)
    return disabled


def fan_out_event_notification(session, notification_type: str, event: dict,
                               data: dict) -> int:
    """Deliver one event notification to every in-game recipient.

    The ``notification_queue`` row this mirrors carries only the *acting*
    player; the in-game audience is the whole team (or all event
    participants), resolved here at delivery time. Returns the number of
    inboxes pushed. Never raises — callers sit on the event-apply path.

    ``session`` is accepted for signature symmetry with the engine helpers but
    the reads run on a dedicated short-lived session: this is called
    mid-transaction on the event-apply session, and a failed read there
    (missing table, transient DB error) would poison the caller's transaction
    with a pending rollback. Rosters/prefs are written outside the apply
    transaction, so a fresh session always sees them.
    """
    try:
        audience = AUDIENCE_FOR_TYPE.get(notification_type)
        if audience is None:
            return 0
        event_id = event.get("id") if isinstance(event, dict) else None
        if audience == AUDIENCE_TEAM:
            team_id = (data or {}).get("team_id")
            if team_id is None:
                return 0
        elif event_id is None:
            return 0

        from db.models.base import Session

        read_session = Session()
        try:
            if audience == AUDIENCE_TEAM:
                player_ids = _team_player_ids(read_session, team_id)
            else:
                player_ids = _event_player_ids(read_session, event_id)
            if not player_ids:
                return 0
            disabled = _players_with_type_disabled(
                read_session, notification_type, player_ids)
        finally:
            try:
                read_session.rollback()
            except Exception:
                pass
            read_session.close()

        envelope = build_envelope(notification_type, data, event=event)
        delivered = 0
        for pid in player_ids:
            if pid in disabled:
                continue
            if push_to_inbox(pid, envelope):
                delivered += 1
        return delivered
    except Exception as e:
        print(f"[plugin_notifications] fan-out failed for {notification_type}: {e}")
        return 0
