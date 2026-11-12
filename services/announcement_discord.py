"""Keep a group announcement's Discord post in step with the website.

The website is canonical; the Discord post is a copy made through the outbox
(``services/discord_outbox``). Before this module, editing an announcement
changed only the website and archiving left the post up in the channel.

* :func:`sync_edit` - a title/body/image change. A post still waiting in the
  outbox is rewritten in place; a post already sent (or being sent) gets an
  ``edit_announcement`` row that the bot applies to the message, keeping the
  rest of the embed (colour, footer, thumbnail...) as it was posted.
* :func:`sync_archive` - a waiting post is cancelled; a sent one gets a
  ``delete_own_message`` row.
* :func:`discord_states` - what the admin list shows per post: posted,
  sending, failed (and roughly why), off, or never sent.

Group announcements only: a site-wide post goes out through HQ's news channel
and is published to following servers (``services/leader_updates``), where an
edit or delete is a deliberate owner action, not a side effect.

Module level is stdlib-only (the unit tests load this file directly); models
and the outbox are imported inside the functions.
"""
from __future__ import annotations

import json

#: The outbox kind of the original post (``web_api/routes/announcements``).
SEND_KIND = "announcement"
EDIT_KIND = "edit_announcement"
DELETE_KIND = "delete_own_message"
#: A waiting post the author archived before the bot got to it.
CANCELLED = "cancelled"

#: discord_states values.
POSTED = "posted"
SENDING = "sending"
FAILED = "failed"
OFF = "off"
NOT_SENT = "not_sent"

#: Failure reasons shown to admins - classified, never the raw error text.
REASON_PERMISSIONS = "missing_permissions"
REASON_CHANNEL = "channel_missing"
REASON_OTHER = "error"


def classify_error(error) -> str:
    text = str(error or "").lower()
    if any(k in text for k in ("missing permissions", "missing access", "403", "forbidden")):
        return REASON_PERMISSIONS
    if any(k in text for k in ("unknown channel", "not found", "404")):
        return REASON_CHANNEL
    return REASON_OTHER


def embed_patch(title=None, body_md=None, cover_image_url=...) -> dict:
    """The fields an edit changes, in Discord embed shape. ``cover_image_url``
    is a sentinel default: absent means "leave the image", ``None`` means
    "remove it"."""
    patch = {}
    if title is not None:
        patch["title"] = title
    if body_md is not None:
        patch["description"] = body_md[:4000]
    if cover_image_url is not ...:
        patch["image"] = {"url": cover_image_url} if cover_image_url else None
    return patch


def apply_patch(embed: dict, patch: dict) -> dict:
    """``embed`` with ``patch`` applied; a ``None`` value removes the key."""
    out = dict(embed or {})
    for key, value in (patch or {}).items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _send_rows(session, ann_id):
    from db.models import DiscordOutbox

    return (
        session.query(DiscordOutbox)
        .filter(DiscordOutbox.ref_type == "announcement",
                DiscordOutbox.ref_id == int(ann_id),
                DiscordOutbox.kind == SEND_KIND)
        .order_by(DiscordOutbox.id.desc())
        .all()
    )


def _target(ann, rows):
    """``(channel_id, message_id)`` the follow-up should act on, ``None`` when
    there is no post to act on. The message id may be ``None`` while the send
    is in flight: the drain resolves it from the announcement, which it writes
    back before reaching a later row."""
    if getattr(ann, "discord_message_id", None):
        return str(ann.discord_channel_id or (rows[0].channel_id if rows else "")), str(ann.discord_message_id)
    live = [r for r in rows if r.status in ("sending", "sent")]
    if live:
        return str(live[0].channel_id), live[0].discord_message_id
    return None


def sync_edit(session, ann, patch: dict) -> str:
    """Carry an edit to Discord. Returns what happened: ``updated_pending``,
    ``queued``, or ``none`` (nothing posted, or nothing that shows changed).
    The caller commits."""
    if not patch or getattr(ann, "scope_type", None) != "group":
        return "none"
    rows = _send_rows(session, ann.id)
    waiting = [r for r in rows if r.status == "pending"]
    if waiting:
        for row in waiting:
            try:
                embed = json.loads(row.embed_json or "{}")
            except (TypeError, ValueError):
                embed = {}
            row.embed_json = json.dumps(apply_patch(embed, patch))
        return "updated_pending"
    target = _target(ann, rows)
    if target is None:
        return "none"
    from services.discord_outbox import enqueue

    channel_id, message_id = target
    enqueue(session, channel_id=channel_id, embed=patch, kind=EDIT_KIND,
            ref_type="announcement", ref_id=ann.id, discord_message_id=message_id,
            commit=False)
    return "queued"


def sync_archive(session, ann) -> str:
    """Take an archived announcement's post down. Returns ``cancelled``,
    ``queued`` or ``none``. The caller commits."""
    if getattr(ann, "scope_type", None) != "group":
        return "none"
    rows = _send_rows(session, ann.id)
    waiting = [r for r in rows if r.status == "pending"]
    for row in waiting:
        row.status = CANCELLED
        row.error = "announcement archived before it was posted"
    target = _target(ann, rows)
    if target is None:
        return "cancelled" if waiting else "none"
    from services.discord_outbox import enqueue

    channel_id, message_id = target
    enqueue(session, channel_id=channel_id, kind=DELETE_KIND, ref_type="announcement",
            ref_id=ann.id, discord_message_id=message_id, commit=False)
    return "queued"


def state_for(ann, rows) -> dict:
    """One announcement's Discord state (rows: its send rows, newest first)."""
    if getattr(ann, "discord_message_id", None):
        return {"status": POSTED}
    if rows:
        latest = rows[0]
        if latest.status in ("pending", "sending"):
            return {"status": SENDING}
        if latest.status == "failed":
            return {"status": FAILED, "reason": classify_error(latest.error)}
        if latest.status == "sent":
            # Sent with nothing written back (the bot sent no message).
            return {"status": FAILED, "reason": REASON_OTHER}
    if not getattr(ann, "post_to_discord", False):
        return {"status": OFF}
    return {"status": NOT_SENT}


def discord_states(session, anns) -> dict:
    """``{announcement_id: state}`` for group announcements, one query."""
    anns = [a for a in anns if getattr(a, "scope_type", None) == "group"]
    if not anns:
        return {}
    from db.models import DiscordOutbox

    by_ann: dict = {}
    for row in (
        session.query(DiscordOutbox)
        .filter(DiscordOutbox.ref_type == "announcement",
                DiscordOutbox.ref_id.in_([a.id for a in anns]),
                DiscordOutbox.kind == SEND_KIND)
        .order_by(DiscordOutbox.id.desc())
        .all()
    ):
        by_ann.setdefault(int(row.ref_id), []).append(row)
    return {a.id: state_for(a, by_ann.get(a.id, [])) for a in anns}
