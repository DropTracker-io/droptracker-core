"""Fallback delivery for the operational event alerts (bug report #126).

``event_activation_failed`` and ``event_end_failed`` are the two event
messages that exist purely to tell a human something is broken. They were
routed to the event's ``admin`` Discord channel and nowhere else, so both ways
that channel can be unusable — never configured, or the bot lacking permission
to post in it — ended with the alert marked ``skipped``/``failed`` and NOBODY
told. A scheduled event failed to activate every minute for four days that way
(2026-08-17); the group's leaders learnt about it from a player.

So: when the channel post does not land, the same alert is DM'd to the group's
leadership through the ``discord_outbox`` DM pipeline (which already handles
recipients with closed DMs). The alert is enqueued at most once per event per
week upstream (the Redis NX guard in ``services/event_lifecycle.py``), so this
fan-out inherits that rate limit rather than adding its own.

Import-time is stdlib-only (DB access is lazy, inside the functions) so the
module loads under the unit-test conftest.
"""
from __future__ import annotations

from typing import Optional

# The alert types worth chasing a human for. Deliberately narrow: these are
# "your event is broken and only you can fix it" messages, not gameplay noise.
# A type added here starts DMing group leaders, so keep the bar that high.
OPERATIONAL_ALERT_TYPES = frozenset({
    "event_activation_failed",
    "event_end_failed",
})

# Upper bound on the fan-out. Leadership lists are single digits in practice;
# this only stops a pathological group from turning one alert into a hundred
# DMs (and tripping Discord's per-bot DM limits for everyone else).
MAX_ALERT_RECIPIENTS = 10


# ══════════════════════════════════════════════════════════════════════════════
# Pure helpers (no I/O — unit-tested in isolation)
# ══════════════════════════════════════════════════════════════════════════════

def order_recipients(owner_ids, admin_ids, manager_ids, authed_ids,
                     limit: int = MAX_ALERT_RECIPIENTS) -> list:
    """Leadership Discord ids, best-first and deduped.

    Order is who-can-actually-fix-it first: the owner, then group admins, then
    event managers (web64a — they manage events but not the group), then the
    bot's legacy ``authed_users`` config as a safety net for groups that
    predate the web grants and have no ``group_admins`` rows at all.
    """
    out, seen = [], set()
    for bucket in (owner_ids, admin_ids, manager_ids, authed_ids):
        for raw in bucket or []:
            if raw is None:
                continue
            did = str(raw).strip()
            # Discord ids are numeric strings; anything else is a data bug and
            # would only produce a doomed fetch_user call.
            if not did.isdigit() or did in seen:
                continue
            seen.add(did)
            out.append(did)
            if len(out) >= limit:
                return out
    return out


def alert_dm_embed(notification_type: str, data: dict,
                   undelivered_reason: Optional[str] = None) -> dict:
    """The alert as a Discord embed dict (``Embed.from_dict`` shape).

    Reuses :func:`services.event_notifications.event_embed_spec` so the DM says
    exactly what the channel post would have said, plus a line explaining why
    it arrived as a DM — otherwise a leader gets an out-of-context warning with
    no idea their alert channel is the actual problem.
    """
    from services.event_notifications import event_embed_spec

    spec = event_embed_spec(notification_type, data)
    embed: dict = {}
    for key in ("title", "description", "url"):
        if spec.get(key):
            embed[key] = spec[key]
    if spec.get("color") is not None:
        embed["color"] = spec["color"]

    fields = [
        {"name": f.get("name"), "value": f.get("value"),
         "inline": bool(f.get("inline"))}
        for f in (spec.get("fields") or [])
        if f.get("name") and f.get("value")
    ]
    if undelivered_reason:
        fields.append({
            "name": "Why this came as a DM",
            "value": undelivered_reason,
            "inline": False,
        })
    if fields:
        embed["fields"] = fields
    return embed


def undelivered_reason_text(reason_code: str) -> str:
    """Human wording for why the channel post didn't land, keyed by the
    delivery outcome the notification service saw."""
    if reason_code == "forbidden":
        return ("This event's Discord alert channel exists, but the "
                "DropTracker bot isn't allowed to post in it. Give it **View "
                "Channel**, **Send Messages** and **Embed Links** there and "
                "these alerts go back to the channel.")
    if reason_code == "no_channel":
        return ("This event has no Discord channel configured for admin "
                "alerts (or they're muted), so there was nowhere to post it. "
                "Set one under Event → Discord.")
    return ("The alert could not be posted to this event's Discord channel, "
            "so it was sent to the group's leaders directly.")


# ══════════════════════════════════════════════════════════════════════════════
# Delivery
# ══════════════════════════════════════════════════════════════════════════════

def alert_recipient_discord_ids(session, group_id) -> list:
    """Discord user ids for ``group_id``'s leadership (see
    :func:`order_recipients` for the ordering contract)."""
    if not group_id:
        return []  # global/superadmin events have no group leadership

    from db.models import GroupAdmin, GroupConfiguration, GroupEventManager, User

    owner_ids, admin_ids = [], []
    rows = (
        session.query(GroupAdmin.role, User.discord_id)
        .join(User, User.user_id == GroupAdmin.user_id)
        .filter(GroupAdmin.group_id == group_id)
        .all()
    )
    for role, discord_id in rows:
        (owner_ids if role == "owner" else admin_ids).append(discord_id)

    manager_ids = [
        discord_id for (discord_id,) in
        session.query(User.discord_id)
        .join(GroupEventManager, GroupEventManager.user_id == User.user_id)
        .filter(GroupEventManager.group_id == group_id)
        .all()
    ]

    # Legacy safety net: the bot-side authorized list. Long lists spill from
    # config_value into long_value (see utils/group_config.py), so read both.
    authed_ids = []
    try:
        import json

        row = (
            session.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == "authed_users")
            .first()
        )
        raw = row and (row.config_value or getattr(row, "long_value", None))
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                authed_ids = parsed
    except Exception:
        authed_ids = []

    return order_recipients(owner_ids, admin_ids, manager_ids, authed_ids)


def enqueue_alert_dms(session, event, notification_type: str, data: dict,
                      reason_code: str) -> int:
    """DM an undeliverable operational alert to the group's leadership.

    Returns how many DM rows were enqueued (0 when the type isn't an
    operational alert or nobody could be resolved). Never raises — this is a
    fallback hanging off a delivery failure and must not become one.
    """
    if notification_type not in OPERATIONAL_ALERT_TYPES:
        return 0
    try:
        from db.app_logger import AppLogger
        from services.discord_outbox import enqueue
        from services.event_notifications import event_url

        app_logger = AppLogger()
        group_id = getattr(event, "group_id", None)
        event_id = getattr(event, "id", None)
        # Surface the delivery failure as a group notice too (web102a): the
        # widget shows it to every group admin even when nobody is DM-able,
        # and it auto-resolves once a notification lands again. Best-effort.
        if group_id:
            try:
                from services.group_notices import raise_group_notice

                raise_group_notice(
                    session,
                    group_id=int(group_id),
                    code=f"event_alert_{reason_code}",
                    title="Event notifications for your group are failing",
                    body=(
                        f"An event alert (`{notification_type}`) could not be "
                        "posted to your configured event channel "
                        f"({'missing permissions' if reason_code == 'forbidden' else 'no usable channel'}). "
                        "Check the event's Discord settings and the bot's channel "
                        "permissions; this notice closes itself once delivery "
                        "recovers."
                    ),
                    data={
                        "event_id": event_id,
                        "notification_type": notification_type,
                        "reason": reason_code,
                    },
                )
            except Exception:
                pass
        recipients = alert_recipient_discord_ids(session, group_id)
        if not recipients:
            # Nothing more this process can do — but say so loudly rather than
            # dropping it the way the channel post did.
            app_logger.log(
                log_type="error",
                data=(f"Event {event_id}: '{notification_type}' could not be "
                      f"delivered to Discord ({reason_code}) and group "
                      f"{group_id} has no resolvable leadership to DM."),
                app_name="event_alerts",
                description="enqueue_alert_dms",
            )
            return 0

        payload = dict(data or {})
        payload.setdefault("event_id", event_id)
        payload.setdefault("event_name", getattr(event, "name", None) or "Event")
        embed = alert_dm_embed(notification_type, payload,
                              undelivered_reason_text(reason_code))
        components = ([{"label": "Open the event manager",
                        "url": event_url(event_id)}] if event_id else None)

        sent = 0
        for discord_id in recipients:
            try:
                enqueue(
                    session,
                    channel_id=discord_id,
                    embed=embed,
                    components=components,
                    kind="dm",
                    ref_type="event",
                    ref_id=event_id,
                    commit=False,
                )
                sent += 1
            except Exception:
                continue
        if sent:
            session.commit()
            app_logger.log(
                log_type="info",
                data=(f"Event {event_id}: '{notification_type}' undeliverable "
                      f"to Discord ({reason_code}) — DM'd {sent} group "
                      f"leader(s) instead."),
                app_name="event_alerts",
                description="enqueue_alert_dms",
            )
        return sent
    except Exception as e:  # noqa: BLE001
        try:
            session.rollback()
        except Exception:
            pass
        try:
            from db.app_logger import AppLogger

            AppLogger().log(
                log_type="error",
                data=f"Alert DM fallback failed for event "
                     f"{getattr(event, 'id', '?')}: {e}",
                app_name="event_alerts",
                description="enqueue_alert_dms",
            )
        except Exception:
            pass
        return 0
