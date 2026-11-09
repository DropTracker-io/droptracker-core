"""Discord audit log: the pure half (no interactions, no DB).

``services/discord_audit.py`` is the bot extension; everything it decides or
renders lives here so it can be tested without Discord:

* which events a server logs (``DEFAULT_EVENTS`` + per-server overrides),
* which messages are stored at all (``should_store``),
* who deleted a message (``DeleteAttribution``, from Discord's audit log),
* what a member-update audit entry means (``describe_member_update``),
* the embeds posted to the audit channel (``*_embed``) and the bulk-delete
  transcript.

Embeds are plain dicts in Discord's API shape. Audit posts are always sent
with pings disabled, and the embed text never needs escaping for that reason:
a mention inside an embed never pings anyone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

#: Every kind of event the audit channel can show, and whether it is on by
#: default. A server's ``events`` JSON only stores the ones it changed.
DEFAULT_EVENTS: dict[str, bool] = {
    "message_delete": True,
    "message_edit": True,
    "message_bulk_delete": True,
    "member_join": True,
    "member_leave": True,
    "member_kick": True,
    "member_ban": True,
    "member_unban": True,
    "member_timeout": True,
    "member_nickname": True,
    "member_roles": True,
}

EVENT_LABELS: dict[str, str] = {
    "message_delete": "Deleted messages",
    "message_edit": "Edited messages",
    "message_bulk_delete": "Bulk deletes",
    "member_join": "Joins",
    "member_leave": "Leaves",
    "member_kick": "Kicks",
    "member_ban": "Bans",
    "member_unban": "Unbans",
    "member_timeout": "Timeouts",
    "member_nickname": "Nickname changes",
    "member_roles": "Role changes",
}

DEFAULT_RETENTION_DAYS = 30
#: Image copies are kept for a shorter time than message text.
ATTACHMENT_RETENTION_DAYS = 7
#: Images larger than this are not copied (the bot can't re-upload them anyway).
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
#: Discord accounts younger than this are flagged on join.
NEW_ACCOUNT_DAYS = 7

# Discord audit log action types (AuditLogEventType).
AUDIT_MEMBER_KICK = 20
AUDIT_MEMBER_BAN_ADD = 22
AUDIT_MEMBER_BAN_REMOVE = 23
AUDIT_MEMBER_UPDATE = 24
AUDIT_MEMBER_ROLE_UPDATE = 25
AUDIT_MESSAGE_DELETE = 72

# Embed colours.
COLOR_DELETE = 0xE74C3C
COLOR_EDIT = 0xF1C40F
COLOR_JOIN = 0x2ECC71
COLOR_LEAVE = 0x95A5A6
COLOR_MOD = 0xE67E22
COLOR_INFO = 0x3498DB

DISCORD_EPOCH_MS = 1420070400000


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def resolve_events(overrides: Optional[dict]) -> dict[str, bool]:
    """Every event kind -> on/off, applying a server's stored overrides.
    Unknown keys in the stored JSON are ignored."""
    events = dict(DEFAULT_EVENTS)
    for key, value in (overrides or {}).items():
        if key in events:
            events[key] = bool(value)
    return events


def set_event(overrides: Optional[dict], kind: str, on: bool) -> dict:
    """New overrides dict with ``kind`` switched; a value equal to the default
    is dropped so the stored JSON only holds real changes."""
    if kind not in DEFAULT_EVENTS:
        raise ValueError(f"unknown audit event {kind!r}")
    out = {k: bool(v) for k, v in (overrides or {}).items() if k in DEFAULT_EVENTS}
    if bool(on) == DEFAULT_EVENTS[kind]:
        out.pop(kind, None)
    else:
        out[kind] = bool(on)
    return out


@dataclass(frozen=True)
class AuditSettings:
    """A server's audit settings, as the extension uses them."""
    guild_id: str
    enabled: bool
    channel_id: Optional[str]
    events: dict
    ignored_channel_ids: frozenset
    retention_days: int

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.channel_id)

    def logs(self, kind: str) -> bool:
        return self.active and bool(self.events.get(kind))

    @classmethod
    def from_row(cls, row) -> "AuditSettings":
        return cls(
            guild_id=str(row.guild_id),
            enabled=bool(row.enabled),
            channel_id=str(row.channel_id) if row.channel_id else None,
            events=resolve_events(row.events),
            ignored_channel_ids=frozenset(str(c) for c in (row.ignored_channel_ids or [])),
            retention_days=max(1, int(row.retention_days or DEFAULT_RETENTION_DAYS)),
        )


def should_store(settings: Optional[AuditSettings], *, author_is_bot: bool,
                 is_webhook: bool, channel_id, parent_channel_id=None) -> bool:
    """Is this message one the bot keeps a copy of?

    Only human messages, only in servers with auditing on, never in the audit
    channel itself or an ignored channel (a thread counts as its parent)."""
    if settings is None or not settings.active:
        return False
    if author_is_bot or is_webhook:
        return False
    return not is_ignored_channel(settings, channel_id, parent_channel_id)


def is_ignored_channel(settings: AuditSettings, channel_id, parent_channel_id=None) -> bool:
    ids = {str(c) for c in (channel_id, parent_channel_id) if c}
    if settings.channel_id and settings.channel_id in ids:
        return True
    return bool(ids & settings.ignored_channel_ids)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def snowflake_ms(snowflake) -> int:
    """Unix milliseconds a Discord id was created at."""
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


def clip(text: Optional[str], limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def human_age(seconds: float) -> str:
    """'45 seconds' / '3 minutes' / '2 hours' / '5 days'."""
    seconds = max(0, int(seconds))
    for size, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {name}{'' if n == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


def _unix(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return int(value)


def _ts(value, style: str = "f") -> str:
    unix = _unix(value)
    return f"<t:{unix}:{style}>" if unix is not None else "unknown"


def _iso_now(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(),
                                  tz=timezone.utc).isoformat()


def _footer(text: str) -> dict:
    return {"text": text}


def _attachment_lines(attachments: Optional[list]) -> str:
    lines = []
    for att in attachments or []:
        name = att.get("filename") or "file"
        size = att.get("size")
        kept = " (copy attached)" if att.get("path") else ""
        size_txt = f", {round(size / 1024)} KB" if size else ""
        lines.append(f"`{clip(name, 80)}`{size_txt}{kept}")
    return "\n".join(lines)


def message_link(guild_id, channel_id, message_id) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


# --------------------------------------------------------------------------- #
# Who deleted a message
# --------------------------------------------------------------------------- #

@dataclass
class DeleteAttribution:
    """Works out who deleted a message from Discord's audit log.

    Discord logs a deletion only when someone deletes ANOTHER person's message
    (a self-delete leaves no entry), and it folds repeat deletions by the same
    moderator, of the same author, in the same channel, into one entry whose
    ``count`` goes up. So each look at the log is compared with the previous
    look: a new entry, or a higher count on a known one, is a credit of N
    deletions for (author, channel) by that moderator. Each deleted message
    then takes one credit. No credit means the author deleted it.

    One instance per server; the caller serialises calls per server.
    """
    seen_counts: dict = field(default_factory=dict)   # entry id -> count
    credits: list = field(default_factory=list)       # [author, channel, mod, remaining, at]
    #: A never-seen entry only counts if it was created this recently; older
    #: unseen entries are from before the bot started watching.
    fresh_seconds: int = 120
    credit_ttl_seconds: int = 120

    def absorb(self, entries: Iterable[dict], now: Optional[float] = None) -> None:
        """Fold one read of the audit log (MESSAGE_DELETE entries) in."""
        now = time.time() if now is None else now
        for entry in entries:
            try:
                entry_id = str(int(entry["id"]))
                count = int((entry.get("options") or {}).get("count") or 1)
                author = str(entry.get("target_id") or "")
                channel = str((entry.get("options") or {}).get("channel_id") or "")
                mod = str(entry.get("user_id") or "")
            except (KeyError, TypeError, ValueError):
                continue
            previous = self.seen_counts.get(entry_id)
            if previous is None:
                fresh = (now * 1000 - snowflake_ms(entry_id)) <= self.fresh_seconds * 1000
                delta = count if fresh else 0
            else:
                delta = count - previous
            self.seen_counts[entry_id] = count
            if delta > 0 and author and mod:
                self.credits.append([author, channel, mod, delta, now])
        self.credits = [c for c in self.credits
                        if c[3] > 0 and now - c[4] <= self.credit_ttl_seconds]
        if len(self.seen_counts) > 500:
            # Keep the newest entries; snowflakes sort by time.
            keep = sorted(self.seen_counts, key=int)[-200:]
            self.seen_counts = {k: self.seen_counts[k] for k in keep}

    def take(self, author_id, channel_id) -> Optional[str]:
        """The moderator who deleted a message by ``author_id`` in
        ``channel_id``, consuming one credit; None = no moderator logged."""
        author, channel = str(author_id), str(channel_id)
        for credit in self.credits:
            if credit[0] == author and credit[1] == channel and credit[3] > 0:
                credit[3] -= 1
                return credit[2]
        return None


# --------------------------------------------------------------------------- #
# Member-update audit entries
# --------------------------------------------------------------------------- #

def _changes(entry: dict) -> dict:
    out = {}
    for change in entry.get("changes") or []:
        if isinstance(change, dict) and change.get("key"):
            out[change["key"]] = change
    return out


def describe_member_update(entry: dict) -> list[tuple[str, dict]]:
    """A MEMBER_UPDATE (24) or MEMBER_ROLE_UPDATE (25) audit entry -> the
    audit events it holds, as (event kind, details)."""
    action = int(entry.get("action_type") or 0)
    changes = _changes(entry)
    out: list[tuple[str, dict]] = []
    if action == AUDIT_MEMBER_UPDATE:
        if "nick" in changes:
            c = changes["nick"]
            out.append(("member_nickname", {"before": c.get("old_value"), "after": c.get("new_value")}))
        if "communication_disabled_until" in changes:
            until = changes["communication_disabled_until"].get("new_value")
            out.append(("member_timeout", {"until": until}))
    elif action == AUDIT_MEMBER_ROLE_UPDATE:
        added = [r for r in (changes.get("$add", {}).get("new_value") or []) if isinstance(r, dict)]
        removed = [r for r in (changes.get("$remove", {}).get("new_value") or []) if isinstance(r, dict)]
        if added or removed:
            out.append(("member_roles", {"added": added, "removed": removed}))
    return out


def parse_discord_time(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #

def _author_line(author_id, author_name: Optional[str]) -> str:
    name = f" ({clip(author_name, 60)})" if author_name else ""
    return f"<@{author_id}>{name}"


def message_delete_embed(row: dict, *, deleted_by: Optional[str], attribution_known: bool,
                         now: Optional[float] = None) -> dict:
    """``row`` is a stored message: message_id, guild_id, channel_id, author_id,
    author_name, content, attachments, created_at, edited_at."""
    now = time.time() if now is None else now
    created = _unix(row.get("created_at"))
    age = human_age(now - created) if created else "unknown"
    if deleted_by and str(deleted_by) != str(row.get("author_id")):
        by = f"<@{deleted_by}>"
    elif attribution_known:
        by = "The author"
    else:
        by = "Unknown (couldn't read the audit log)"
    content = row.get("content") or ""
    description = clip(content, 3800) if content else "*No text*"
    fields = [
        {"name": "Author", "value": _author_line(row.get("author_id"), row.get("author_name")), "inline": True},
        {"name": "Channel", "value": f"<#{row.get('channel_id')}>", "inline": True},
        {"name": "Deleted by", "value": by, "inline": True},
        {"name": "Posted", "value": f"{_ts(created)} ({age} before it was deleted)", "inline": False},
    ]
    if row.get("edited_at"):
        fields.append({"name": "Last edited", "value": _ts(row["edited_at"]), "inline": False})
    files = _attachment_lines(row.get("attachments"))
    if files:
        fields.append({"name": "Attachments", "value": clip(files, 1024), "inline": False})
    return {
        "title": "Message deleted",
        "description": description,
        "color": COLOR_DELETE,
        "fields": fields,
        "footer": _footer(f"Author ID {row.get('author_id')} | Message ID {row.get('message_id')}"),
        "timestamp": _iso_now(now),
    }


def message_edit_embed(row: Optional[dict], *, guild_id, channel_id, message_id, author_id,
                       author_name: Optional[str], after: str, now: Optional[float] = None) -> dict:
    """``row`` is the stored copy before the edit, or None if the message was
    posted before the bot started keeping copies."""
    before = (row or {}).get("content")
    if row is None:
        before_txt = "*Not recorded (posted before logging started)*"
    else:
        before_txt = clip(before, 1900) if before else "*No text*"
    after_txt = clip(after, 1900) if after else "*No text*"
    return {
        "title": "Message edited",
        "url": message_link(guild_id, channel_id, message_id),
        "description": f"**Before**\n{before_txt}\n\n**After**\n{after_txt}",
        "color": COLOR_EDIT,
        "fields": [
            {"name": "Author", "value": _author_line(author_id, author_name), "inline": True},
            {"name": "Channel", "value": f"<#{channel_id}>", "inline": True},
        ],
        "footer": _footer(f"Author ID {author_id} | Message ID {message_id}"),
        "timestamp": _iso_now(now),
    }


def bulk_delete_embed(*, channel_id, total: int, recorded: int, deleted_by: Optional[str],
                      now: Optional[float] = None) -> dict:
    by = f"<@{deleted_by}>" if deleted_by else "Unknown"
    note = ("The text of the recorded ones is in the attached file."
            if recorded else "None of them were recorded.")
    return {
        "title": "Messages bulk deleted",
        "description": f"**{total}** messages were deleted in <#{channel_id}>. "
                       f"**{recorded}** of them were recorded. {note}",
        "color": COLOR_DELETE,
        "fields": [{"name": "Deleted by", "value": by, "inline": True}],
        "timestamp": _iso_now(now),
    }


def bulk_delete_transcript(rows: list[dict], channel_name: Optional[str] = None) -> str:
    """Plain-text record of the stored messages from a bulk delete, oldest first."""
    header = f"Bulk delete in #{channel_name}" if channel_name else "Bulk delete"
    lines = [header, ""]
    for row in sorted(rows, key=lambda r: int(r.get("message_id") or 0)):
        created = row.get("created_at")
        stamp = created.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(created, datetime) else "?"
        who = f"{row.get('author_name') or '?'} ({row.get('author_id')})"
        lines.append(f"[{stamp}] {who}: {row.get('content') or ''}")
        for att in row.get("attachments") or []:
            lines.append(f"    attachment: {att.get('filename')} {att.get('url') or ''}".rstrip())
    return "\n".join(lines) + "\n"


def member_join_embed(*, user_id, username: Optional[str], now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    created_s = snowflake_ms(user_id) / 1000
    age_s = now - created_s
    fields = [
        {"name": "Member", "value": _author_line(user_id, username), "inline": True},
        {"name": "Account created", "value": f"{_ts(int(created_s))} ({human_age(age_s)} ago)", "inline": True},
    ]
    title = "Member joined"
    if age_s < NEW_ACCOUNT_DAYS * 86400:
        title = "Member joined (new account)"
    return {
        "title": title,
        "color": COLOR_JOIN,
        "fields": fields,
        "footer": _footer(f"User ID {user_id}"),
        "timestamp": _iso_now(now),
    }


def member_leave_embed(*, user_id, username: Optional[str], joined_at=None,
                       now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    fields = [{"name": "Member", "value": _author_line(user_id, username), "inline": True}]
    joined = _unix(joined_at)
    if joined:
        fields.append({"name": "Joined", "value": f"{_ts(joined)} ({human_age(now - joined)} ago)",
                       "inline": True})
    return {
        "title": "Member left",
        "color": COLOR_LEAVE,
        "fields": fields,
        "footer": _footer(f"User ID {user_id}"),
        "timestamp": _iso_now(now),
    }


_MOD_TITLES = {
    "member_kick": "Member kicked",
    "member_ban": "Member banned",
    "member_unban": "Member unbanned",
}


def moderation_embed(kind: str, *, target_id, actor_id, reason: Optional[str],
                     target_name: Optional[str] = None, now: Optional[float] = None) -> dict:
    fields = [
        {"name": "Member", "value": _author_line(target_id, target_name), "inline": True},
        {"name": "By", "value": f"<@{actor_id}>" if actor_id else "Unknown", "inline": True},
        {"name": "Reason", "value": clip(reason, 1024) if reason else "*None given*", "inline": False},
    ]
    return {
        "title": _MOD_TITLES.get(kind, kind),
        "color": COLOR_MOD,
        "fields": fields,
        "footer": _footer(f"User ID {target_id}"),
        "timestamp": _iso_now(now),
    }


def member_update_embed(kind: str, details: dict, *, target_id, actor_id,
                        reason: Optional[str] = None, now: Optional[float] = None) -> dict:
    by = f"<@{actor_id}>" if actor_id else "Unknown"
    if str(actor_id) == str(target_id):
        by = "Themselves"
    fields = [
        {"name": "Member", "value": f"<@{target_id}>", "inline": True},
        {"name": "By", "value": by, "inline": True},
    ]
    if kind == "member_nickname":
        title = "Nickname changed"
        before = details.get("before") or "*None*"
        after = details.get("after") or "*None (reset)*"
        fields.append({"name": "Before", "value": clip(before, 1024), "inline": False})
        fields.append({"name": "After", "value": clip(after, 1024), "inline": False})
        color = COLOR_INFO
    elif kind == "member_timeout":
        until = parse_discord_time(details.get("until"))
        if until:
            title = "Member timed out"
            fields.append({"name": "Until", "value": f"{_ts(until)} ({_ts(until, 'R')})", "inline": False})
        else:
            title = "Timeout removed"
        color = COLOR_MOD
    else:
        title = "Roles changed"
        added = ", ".join(f"<@&{r.get('id')}>" for r in details.get("added") or [])
        removed = ", ".join(f"<@&{r.get('id')}>" for r in details.get("removed") or [])
        if added:
            fields.append({"name": "Added", "value": clip(added, 1024), "inline": False})
        if removed:
            fields.append({"name": "Removed", "value": clip(removed, 1024), "inline": False})
        color = COLOR_INFO
    if reason:
        fields.append({"name": "Reason", "value": clip(reason, 1024), "inline": False})
    return {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": _footer(f"User ID {target_id}"),
        "timestamp": _iso_now(now),
    }


def status_text(settings: Optional[AuditSettings], stored_messages: Optional[int] = None) -> str:
    """The /auditlog status reply."""
    if settings is None or not settings.channel_id:
        return "The audit log is **off** in this server. Turn it on with `/auditlog channel`."
    state = "**on**" if settings.enabled else "**off** (paused)"
    lines = [f"The audit log is {state}, posting to <#{settings.channel_id}>."]
    on = [EVENT_LABELS[k] for k, v in settings.events.items() if v]
    off = [EVENT_LABELS[k] for k, v in settings.events.items() if not v]
    lines.append(f"Logged: {', '.join(on) if on else 'nothing'}.")
    if off:
        lines.append(f"Not logged: {', '.join(off)}.")
    if settings.ignored_channel_ids:
        ignored = ", ".join(f"<#{c}>" for c in sorted(settings.ignored_channel_ids))
        lines.append(f"Ignored channels: {ignored}.")
    lines.append(f"Message copies are kept for {settings.retention_days} days, "
                 f"image copies for {ATTACHMENT_RETENTION_DAYS} days.")
    if stored_messages is not None:
        lines.append(f"Stored messages right now: {stored_messages:,}.")
    return "\n".join(lines)
