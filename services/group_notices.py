"""Bot→group problem notices (web102a).

When the bot hits a per-group problem it can name (notification channel
deleted, permissions revoked, event alerts undeliverable), it raises a
*notice*: one ``group_notices`` row per (group, code) mapped to one long-lived
``group_notice`` chat thread the clan's admins see in the site's chat widget
(their group party seats them automatically) and can reply to. Recurrences
bump the row silently; when the underlying operation succeeds again the
notice auto-resolves with a "this looks fixed" system entry. The point is to
stop the stream of repetitive tickets about problems the bot already knows
about — without becoming its own spam stream.

Spam posture, in order:
  1. Redis ``SET NX EX`` cooldown per (group, code) — a failure loop costs
     one Redis op per pass, no DB writes.
  2. An already-open notice never re-notifies: recurrence = row bump +
     system entry, no DM.
  3. DM fan-out fires only on the open-transition, capped at
     ``event_alerts``'s recipient contract (owner → admins → managers, ≤10).

``raise_group_notice`` / ``resolve_group_notice`` never raise: every caller
is a failure (or success) path of something more important.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from utils.site_urls import WEBSITE_URL

THREAD_KIND = "group_notice"
SUBJECT_TYPE = "group_notice"

DEFAULT_COOLDOWN_SECONDS = 86400

#: Codes the notification-success path may auto-resolve. Kept explicit so a
#: manually raised (superadmin) notice is never closed by a passing drop.
AUTO_RESOLVE_ON_GROUP_SEND = (
    "notify_channel_forbidden",
    "notify_channel_missing",
    "event_alert_forbidden",
    "event_alert_no_channel",
)


def _rc():
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception:
        return None


def _cooldown_key(group_id: int, code: str) -> str:
    return f"notice:cool:{int(group_id)}:{code}"


def _open_flag_key(group_id: int, code: str) -> str:
    return f"notice:open:{int(group_id)}:{code}"


def open_notice_flag(group_id: int, code: str) -> bool:
    """Zero-DB probe for hot paths: is a notice open for this (group, code)?
    Fails closed (False) — a Redis outage must not make success paths query."""
    try:
        conn = _rc()
        return bool(conn and conn.get(_open_flag_key(group_id, code)))
    except Exception:
        return False


def thread_url(thread_id: int) -> str:
    return f"{WEBSITE_URL}/messages/{int(thread_id)}"


def raise_group_notice(
    s,
    *,
    group_id: int,
    code: str,
    title: str,
    body: str,
    severity: str = "major",
    data: Optional[dict] = None,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    dm_recipients: bool = True,
):
    """Open (or bump) the notice for ``(group_id, code)``. Returns the
    ``GroupNotice`` row, or None when the cooldown swallowed the raise.
    Commits on success; never raises."""
    try:
        conn = _rc()
        if conn is not None:
            try:
                if not conn.set(
                    _cooldown_key(group_id, code), "1",
                    nx=True, ex=int(cooldown_seconds),
                ):
                    return None
            except Exception:
                pass  # Redis down: fall through, the DB dedupe still holds

        from db.models import GroupNotice
        from services.chat import get_or_create_thread, post_system

        notice = (
            s.query(GroupNotice)
            .filter(GroupNotice.group_id == int(group_id), GroupNotice.code == code)
            .first()
        )
        payload = {
            "code": code,
            "severity": severity,
            "title": title,
            "body": body,
            **({"data": data} if data else {}),
        }
        if notice is not None and notice.status == "open":
            # Recurrence: silent bump — the thread already tells the story.
            notice.last_raised_at = datetime.now()
            notice.raise_count = int(notice.raise_count or 0) + 1
            if notice.thread_id:
                thread = _thread(s, notice.thread_id)
                if thread is not None:
                    post_system(
                        s,
                        thread=thread,
                        code="notice_recurred",
                        data={"code": code, "raise_count": notice.raise_count},
                        commit=False,
                        publish=False,
                    )
            s.commit()
            _set_open_flag(group_id, code)
            return notice

        reopen = notice is not None
        if notice is None:
            notice = GroupNotice(
                group_id=int(group_id),
                code=code,
                severity=severity,
                title=title[:200],
                status="open",
                data_json=json.dumps(data) if data else None,
            )
            s.add(notice)
            s.flush()
        else:
            notice.status = "open"
            notice.severity = severity
            notice.title = title[:200]
            notice.last_raised_at = datetime.now()
            notice.raise_count = int(notice.raise_count or 0) + 1
            notice.resolved_at = None
            notice.resolved_by_user_id = None
            if data:
                notice.data_json = json.dumps(data)

        thread = get_or_create_thread(
            s,
            kind=THREAD_KIND,
            subject_type=SUBJECT_TYPE,
            subject_id=int(notice.id),
            parties=[("group", int(group_id))],
            title=title[:255],
            commit=False,
        )
        s.flush()
        notice.thread_id = int(thread.id)
        if thread.title != title[:255]:
            thread.title = title[:255]
        sys_row = post_system(
            s,
            thread=thread,
            code="notice_raised",
            data=payload,
            commit=False,
            publish=False,
        )
        s.commit()
        _set_open_flag(group_id, code)
        try:
            from services.chat import publish_message

            publish_message(s, thread, sys_row)
        except Exception:
            pass
        if dm_recipients:
            _queue_notice_dms(
                s, group_id=group_id, thread_id=thread.id,
                title=title, body=body, severity=severity, reopened=reopen,
            )
        return notice
    except Exception as e:  # noqa: BLE001
        try:
            s.rollback()
        except Exception:
            pass
        print(f"[group_notices] raise failed ({group_id}, {code}): {e}")
        return None


def resolve_group_notice(
    s,
    *,
    group_id: int,
    code: str,
    resolved_by_user_id: Optional[int] = None,
    note: Optional[str] = None,
) -> bool:
    """Close the notice and post the "this looks fixed" entry. Cheap when
    nothing is open (one Redis GET). Never raises."""
    try:
        if resolved_by_user_id is None and not open_notice_flag(group_id, code):
            # Hot-path caller and no flag: nothing to do. (A manual resolver
            # skips the probe — Redis may have lost the flag.)
            return False
        from db.models import GroupNotice
        from services.chat import post_system

        notice = (
            s.query(GroupNotice)
            .filter(
                GroupNotice.group_id == int(group_id),
                GroupNotice.code == code,
                GroupNotice.status == "open",
            )
            .first()
        )
        conn = _rc()
        if notice is None:
            if conn is not None:
                try:
                    conn.delete(_open_flag_key(group_id, code))
                except Exception:
                    pass
            return False
        notice.status = "resolved"
        notice.resolved_at = datetime.now()
        notice.resolved_by_user_id = resolved_by_user_id
        sys_row = None
        if notice.thread_id:
            thread = _thread(s, notice.thread_id)
            if thread is not None:
                sys_row = post_system(
                    s,
                    thread=thread,
                    code="notice_resolved",
                    data={
                        "code": code,
                        **({"note": note} if note else {}),
                        "manual": resolved_by_user_id is not None,
                    },
                    actor_user_id=resolved_by_user_id,
                    commit=False,
                    publish=False,
                )
        s.commit()
        if conn is not None:
            try:
                conn.delete(_open_flag_key(group_id, code))
                conn.delete(_cooldown_key(group_id, code))
            except Exception:
                pass
        if sys_row is not None:
            try:
                from services.chat import publish_message

                publish_message(s, _thread(s, notice.thread_id), sys_row)
            except Exception:
                pass
        return True
    except Exception as e:  # noqa: BLE001
        try:
            s.rollback()
        except Exception:
            pass
        print(f"[group_notices] resolve failed ({group_id}, {code}): {e}")
        return False


def auto_resolve_group_send(s, group_id: int) -> None:
    """Success-path hook: a notification just landed in this group's Discord,
    so any open channel/delivery notices are evidently fixed. Flag-guarded —
    the common case costs a few Redis GETs and touches no tables."""
    for code in AUTO_RESOLVE_ON_GROUP_SEND:
        if open_notice_flag(group_id, code):
            resolve_group_notice(s, group_id=group_id, code=code)


def _thread(s, thread_id):
    from db.models import ChatThread

    return s.query(ChatThread).filter(ChatThread.id == int(thread_id)).first()


def _set_open_flag(group_id: int, code: str) -> None:
    try:
        conn = _rc()
        if conn is not None:
            conn.set(_open_flag_key(group_id, code), "1")
    except Exception:
        pass


_SEVERITY_COLORS = {"info": 0x3498DB, "minor": 0xF1C40F, "major": 0xE67E22, "critical": 0xE74C3C}


def _queue_notice_dms(s, *, group_id: int, thread_id: int, title: str,
                      body: str, severity: str, reopened: bool) -> int:
    """DM the group's leadership about a newly opened notice, through the
    outbox. Fires only on the open-transition; recurrences never re-ping."""
    try:
        from db.models import Group
        from services.discord_outbox import enqueue
        from services.event_alerts import alert_recipient_discord_ids

        group = s.query(Group).filter(Group.group_id == int(group_id)).first()
        group_name = getattr(group, "group_name", None) or f"group {group_id}"
        recipients = alert_recipient_discord_ids(s, group_id)
        if not recipients:
            return 0
        embed = {
            "title": title[:250],
            "description": (
                f"{body[:1800]}\n\n"
                f"This notice is about **{group_name}**. You can reply to "
                f"DropTracker staff from the link below; it will update "
                f"automatically once the problem looks fixed."
            ),
            "color": _SEVERITY_COLORS.get(severity, 0xE67E22),
        }
        queued = 0
        for discord_id in recipients:
            enqueue(
                s,
                channel_id=str(discord_id),  # USER id for kind='dm'
                kind="dm",
                embed=embed,
                components=[
                    {"label": "View notice", "url": thread_url(thread_id)}
                ],
                ref_type="group_notice",
                ref_id=int(thread_id),
                commit=False,
            )
            queued += 1
        s.commit()
        return queued
    except Exception as e:  # noqa: BLE001
        try:
            s.rollback()
        except Exception:
            pass
        print(f"[group_notices] DM fan-out failed ({group_id}): {e}")
        return 0
