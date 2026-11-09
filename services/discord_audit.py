"""Discord server audit log (webhook bot extension).

Posts what happens in a server to an admin-only audit channel: deleted and
edited messages (with what they said), bulk deletes, joins, leaves, kicks,
bans, unbans, timeouts, nickname and role changes. Turned on per server with
``/auditlog channel``; the decisions and the embeds live in
``services/discord_audit_core.py``.

Discord only says WHICH message was deleted, so this keeps its own copy of
every human message in a server with auditing on (``discord_message_log``),
plus a private copy of small images (spam is mostly screenshots, and Discord's
image links stop working once a message is gone). Copies are purged after the
server's retention period (text) and ``ATTACHMENT_RETENTION_DAYS`` (images).
The images are kept outside every served directory; they only ever leave the
box as attachments on an audit post.

Built guild-agnostic (settings are per server, keyed by guild id) so the core
bot can load it for clan servers later. Today only the webhook bot loads it,
for the main DropTracker server.

Event-loop rule (see the gateway-stall history in bots/main.py): no DB work on
the loop. Message copies are queued and written in batches from a worker
thread; lookups and settings reads go through ``asyncio.to_thread``.
"""
import asyncio
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Optional

import aiohttp
from interactions import (
    AllowedMentions,
    ChannelType,
    Extension,
    File,
    OptionType,
    Permissions,
    SlashCommandChoice,
    SlashContext,
    listen,
    slash_command,
    slash_option,
)
from interactions.api.events import MemberAdd, MemberRemove, MessageCreate, MessageUpdate, RawGatewayEvent
from sqlalchemy import text
from sqlalchemy.dialects.mysql import insert as mysql_insert

from db.models import DiscordAuditConfig, DiscordMessageLog, Session
from services import discord_audit_core as core
from services.discord_roles import MAIN_GUILD_ID

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Private image copies. NOT under static/ (which /img serves publicly).
AUDIT_FILE_ROOT = os.getenv("DISCORD_AUDIT_FILES_DIR") or os.path.join(
    _REPO_ROOT, "data", "discord_audit_files")

#: Slash commands are registered where this bot runs the audit log. A later
#: core-bot version would register them globally instead.
COMMAND_SCOPES = [MAIN_GUILD_ID]

SETTINGS_TTL_SECONDS = 60
FLUSH_SECONDS = 2
PURGE_SECONDS = 3600
#: Recently seen messages kept in memory, so a delete right after a post
#: doesn't depend on the batch write having landed.
RECENT_MESSAGES = 5000
#: Wait this long after a delete before reading Discord's audit log, so the
#: moderator's entry has been written.
ATTRIBUTION_DELAY_SECONDS = 1.5
#: A leave this soon after a kick/ban of the same member is not posted twice.
LEAVE_GRACE_SECONDS = 3
#: Discord's upload cap for a bot without a boosted server is 10 MB.
MAX_UPLOAD_BYTES = int(9.5 * 1024 * 1024)
#: Posts waiting for the audit channel; beyond this the oldest are dropped.
MAX_POST_QUEUE = 500

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_NO_PINGS = AllowedMentions.none()


def _log(msg: str) -> None:
    print(f"[audit] {msg}")


def _safe_filename(name: str) -> str:
    name = _SAFE_NAME.sub("_", os.path.basename(name or "file"))
    return name[-100:]


def _plain_datetime(value) -> Optional[datetime]:
    """Naive UTC ``datetime`` (PyMySQL only maps exact datetime, and the
    message timestamps are interactions' Timestamp subclass)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return datetime.fromtimestamp(value.timestamp(), tz=timezone.utc).replace(tzinfo=None)
    return None


def _utc_naive(value) -> Optional[datetime]:
    """A naive DB datetime is UTC; tag it so embeds render the right time."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _display_name(user) -> Optional[str]:
    if user is None:
        return None
    return getattr(user, "username", None) or getattr(user, "display_name", None)


# --------------------------------------------------------------------------- #
# DB helpers (worker thread only)
# --------------------------------------------------------------------------- #

def _load_settings(guild_id: str) -> Optional[core.AuditSettings]:
    s = Session()
    try:
        row = s.get(DiscordAuditConfig, str(guild_id))
        return core.AuditSettings.from_row(row) if row else None
    finally:
        s.close()


def _save_settings(guild_id: str, actor_id: str, **changes) -> core.AuditSettings:
    s = Session()
    try:
        row = s.get(DiscordAuditConfig, str(guild_id))
        if row is None:
            row = DiscordAuditConfig(guild_id=str(guild_id), enabled=False,
                                     retention_days=core.DEFAULT_RETENTION_DAYS)
            s.add(row)
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_by = str(actor_id)
        s.commit()
        return core.AuditSettings.from_row(row)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _row_dict(row: DiscordMessageLog) -> dict:
    return {
        "message_id": int(row.message_id),
        "guild_id": int(row.guild_id),
        "channel_id": int(row.channel_id),
        "author_id": int(row.author_id),
        "author_name": row.author_name,
        "content": row.content,
        "attachments": row.attachments or [],
        "created_at": _utc_naive(row.created_at),
        "edited_at": _utc_naive(row.edited_at),
    }


def _fetch_rows(message_ids: list) -> dict:
    s = Session()
    try:
        rows = (s.query(DiscordMessageLog)
                .filter(DiscordMessageLog.message_id.in_([int(m) for m in message_ids]))
                .all())
        return {int(r.message_id): _row_dict(r) for r in rows}
    finally:
        s.close()


def _write_batch(upserts: list, deleted: list) -> None:
    """Upsert message copies, then stamp deletions. An upsert of a message
    that is already stored only refreshes its text and edit time."""
    s = Session()
    try:
        if upserts:
            # One row per message: the latest version wins.
            latest = {}
            for row in upserts:
                latest[row["message_id"]] = row
            values = [{
                "message_id": r["message_id"],
                "guild_id": r["guild_id"],
                "channel_id": r["channel_id"],
                "author_id": r["author_id"],
                "author_name": (r.get("author_name") or "")[:100] or None,
                "content": r.get("content"),
                "attachments": r.get("attachments") or None,
                "created_at": _plain_datetime(r.get("created_at")) or datetime.utcnow(),
                "edited_at": _plain_datetime(r.get("edited_at")),
            } for r in latest.values()]
            stmt = mysql_insert(DiscordMessageLog).values(values)
            stmt = stmt.on_duplicate_key_update(
                content=stmt.inserted.content,
                edited_at=stmt.inserted.edited_at,
                attachments=stmt.inserted.attachments,
            )
            s.execute(stmt)
        if deleted:
            (s.query(DiscordMessageLog)
             .filter(DiscordMessageLog.message_id.in_([int(m) for m in deleted]))
             .update({DiscordMessageLog.deleted_at: datetime.utcnow()}, synchronize_session=False))
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _purge_expired(now: Optional[datetime] = None) -> int:
    """Delete message copies past each server's retention period, and image
    copies older than ATTACHMENT_RETENTION_DAYS. Returns rows deleted."""
    now = now or datetime.utcnow()
    removed = 0
    s = Session()
    try:
        configs = {str(g): int(d or core.DEFAULT_RETENTION_DAYS)
                   for g, d in s.query(DiscordAuditConfig.guild_id, DiscordAuditConfig.retention_days)}
        default_cutoff = now - timedelta(days=core.DEFAULT_RETENTION_DAYS)
        for guild_id, days in configs.items():
            cutoff = now - timedelta(days=max(1, days))
            while True:
                res = s.execute(text(
                    "DELETE FROM discord_message_log WHERE guild_id = :g AND created_at < :c LIMIT 5000"),
                    {"g": int(guild_id), "c": cutoff})
                s.commit()
                removed += res.rowcount or 0
                if (res.rowcount or 0) < 5000:
                    break
        # Servers without a settings row keep the default period.
        known = ",".join(str(int(g)) for g in configs)
        not_known = f" AND guild_id NOT IN ({known})" if known else ""
        while True:
            res = s.execute(text(
                f"DELETE FROM discord_message_log WHERE created_at < :c{not_known} LIMIT 5000"),
                {"c": default_cutoff})
            s.commit()
            removed += res.rowcount or 0
            if (res.rowcount or 0) < 5000:
                break
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
    _purge_files(now)
    return removed


def _purge_files(now: datetime) -> None:
    if not os.path.isdir(AUDIT_FILE_ROOT):
        return
    cutoff = (now - timedelta(days=core.ATTACHMENT_RETENTION_DAYS)).replace(tzinfo=timezone.utc).timestamp()
    for root, dirs, files in os.walk(AUDIT_FILE_ROOT, topdown=False):
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
        if root != AUDIT_FILE_ROOT:
            try:
                os.rmdir(root)  # only succeeds when empty
            except OSError:
                pass


def _forget_author(guild_id: str, author_id: str) -> int:
    """Delete every stored message (and image copy) of one person in one
    server. Returns how many messages were removed."""
    s = Session()
    try:
        rows = (s.query(DiscordMessageLog)
                .filter(DiscordMessageLog.guild_id == int(guild_id),
                        DiscordMessageLog.author_id == int(author_id))
                .all())
        for row in rows:
            for att in row.attachments or []:
                path = _abs_attachment_path(att.get("path"))
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        count = (s.query(DiscordMessageLog)
                 .filter(DiscordMessageLog.guild_id == int(guild_id),
                         DiscordMessageLog.author_id == int(author_id))
                 .delete(synchronize_session=False))
        s.commit()
        return int(count or 0)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _stored_count(guild_id: str) -> int:
    s = Session()
    try:
        return int(s.query(DiscordMessageLog)
                   .filter(DiscordMessageLog.guild_id == int(guild_id)).count())
    finally:
        s.close()


def _abs_attachment_path(rel: Optional[str]) -> Optional[str]:
    """Resolve a stored relative path, refusing anything outside the root."""
    if not rel:
        return None
    root = os.path.realpath(AUDIT_FILE_ROOT)
    path = os.path.realpath(os.path.join(root, rel))
    if not path.startswith(root + os.sep):
        return None
    return path


# --------------------------------------------------------------------------- #
# Extension
# --------------------------------------------------------------------------- #

class DiscordAudit(Extension):
    def __init__(self, bot):
        self.bot = bot
        self._settings_cache: dict = {}      # guild_id -> (settings | None, loaded_at)
        self._recent: OrderedDict = OrderedDict()  # message_id -> row dict
        self._pending_upserts: list = []
        self._pending_deleted: list = []
        self._posts: asyncio.Queue = asyncio.Queue()
        self._attribution: dict = {}         # guild_id -> core.DeleteAttribution
        self._attribution_locks: dict = {}   # guild_id -> asyncio.Lock
        self._recent_mod_actions: dict = {}  # (guild_id, user_id) -> unix time
        self._warned_channels: set = set()
        asyncio.create_task(self._start())

    async def _start(self):
        asyncio.create_task(self._flush_loop())
        asyncio.create_task(self._post_loop())
        asyncio.create_task(self._purge_loop())
        _log(f"loaded; image copies in {AUDIT_FILE_ROOT}")

    # ------------------------------------------------------------------ settings

    async def _settings(self, guild_id) -> Optional[core.AuditSettings]:
        if not guild_id:
            return None
        key = str(guild_id)
        cached = self._settings_cache.get(key)
        if cached and time.monotonic() - cached[1] < SETTINGS_TTL_SECONDS:
            return cached[0]
        try:
            settings = await asyncio.to_thread(_load_settings, key)
        except Exception as e:  # noqa: BLE001
            _log(f"settings read failed for {key}: {e}")
            return cached[0] if cached else None
        self._settings_cache[key] = (settings, time.monotonic())
        return settings

    def _remember_settings(self, settings: core.AuditSettings) -> None:
        self._settings_cache[settings.guild_id] = (settings, time.monotonic())

    # ------------------------------------------------------------------ store

    def _remember(self, row: dict) -> None:
        self._recent[row["message_id"]] = row
        self._recent.move_to_end(row["message_id"])
        while len(self._recent) > RECENT_MESSAGES:
            self._recent.popitem(last=False)

    async def _lookup(self, message_ids: list) -> dict:
        found = {}
        missing = []
        for mid in message_ids:
            row = self._recent.get(int(mid))
            if row is not None:
                found[int(mid)] = row
            else:
                missing.append(int(mid))
        if missing:
            try:
                found.update(await asyncio.to_thread(_fetch_rows, missing))
            except Exception as e:  # noqa: BLE001
                _log(f"message lookup failed: {e}")
        return found

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            if not self._pending_upserts and not self._pending_deleted:
                continue
            upserts, self._pending_upserts = self._pending_upserts, []
            deleted, self._pending_deleted = self._pending_deleted, []
            try:
                await asyncio.to_thread(_write_batch, upserts, deleted)
            except Exception as e:  # noqa: BLE001
                _log(f"write of {len(upserts)} message(s) failed: {e}")
                # Keep them for the next pass, bounded so a dead DB can't grow memory forever.
                self._pending_upserts = (upserts + self._pending_upserts)[-RECENT_MESSAGES:]
                self._pending_deleted = (deleted + self._pending_deleted)[-RECENT_MESSAGES:]

    async def _purge_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                removed = await asyncio.to_thread(_purge_expired)
                if removed:
                    _log(f"purged {removed} expired message copies")
            except Exception as e:  # noqa: BLE001
                _log(f"purge failed: {e}")
            await asyncio.sleep(PURGE_SECONDS)

    async def _copy_images(self, row: dict, attachments: list) -> None:
        """Download small images into the private store, recording each path
        on the row. Failures keep only the (expiring) Discord link."""
        day = datetime.utcnow().strftime("%Y-%m-%d")
        rel_dir = os.path.join(str(row["guild_id"]), day)
        try:
            async with aiohttp.ClientSession() as http:
                for i, att in enumerate(attachments):
                    entry = row["attachments"][i]
                    content_type = (entry.get("content_type") or "").lower()
                    size = entry.get("size") or 0
                    if not content_type.startswith("image/") or size > core.MAX_ATTACHMENT_BYTES:
                        continue
                    rel = os.path.join(rel_dir, f"{row['message_id']}_{i}_{_safe_filename(entry['filename'])}")
                    dest = _abs_attachment_path(rel)
                    if not dest:
                        continue
                    try:
                        async with http.get(entry["url"]) as resp:
                            resp.raise_for_status()
                            data = await resp.read()
                        if len(data) > core.MAX_ATTACHMENT_BYTES:
                            continue
                        await asyncio.to_thread(_write_file, dest, data)
                        entry["path"] = rel
                    except Exception as e:  # noqa: BLE001
                        _log(f"image copy failed (message {row['message_id']}): {e}")
        except Exception as e:  # noqa: BLE001
            _log(f"image copy session failed: {e}")

    # ------------------------------------------------------------------ posting

    def _queue_post(self, settings: core.AuditSettings, embed: dict, files: Optional[list] = None) -> None:
        if self._posts.qsize() >= MAX_POST_QUEUE:
            try:
                self._posts.get_nowait()
            except asyncio.QueueEmpty:
                pass
            _log("post queue full; dropped the oldest audit post")
        self._posts.put_nowait((settings.channel_id, embed, files or []))

    async def _post_loop(self):
        while True:
            channel_id, embed, files = await self._posts.get()
            try:
                await self._send(channel_id, embed, files)
            except Exception as e:  # noqa: BLE001
                _log(f"post to {channel_id} failed: {e}")

    async def _send(self, channel_id: str, embed: dict, files: list) -> None:
        try:
            channel = await self.bot.fetch_channel(int(channel_id))
        except Exception as e:  # noqa: BLE001
            channel = None
            err = e
        else:
            err = None
        if channel is None or not hasattr(channel, "send"):
            if channel_id not in self._warned_channels:
                self._warned_channels.add(channel_id)
                _log(f"audit channel {channel_id} is unreachable: {err}")
            return
        uploads = [File(BytesIO(data), file_name=name) for name, data in files]
        try:
            await channel.send(embeds=[embed], files=uploads or None, allowed_mentions=_NO_PINGS)
        except Exception as e:  # noqa: BLE001
            if not uploads:
                raise
            _log(f"post with {len(uploads)} file(s) failed ({e}); retrying without them")
            await channel.send(embeds=[embed], allowed_mentions=_NO_PINGS)

    @staticmethod
    def _image_files(row: dict) -> list:
        """(name, bytes) for the stored image copies, within the upload cap."""
        out, total = [], 0
        for att in row.get("attachments") or []:
            path = _abs_attachment_path(att.get("path"))
            if not path or not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
                if total + size > MAX_UPLOAD_BYTES or len(out) >= 10:
                    break
                with open(path, "rb") as f:
                    out.append((_safe_filename(att.get("filename") or os.path.basename(path)), f.read()))
                total += size
            except OSError:
                continue
        return out

    # ------------------------------------------------------------------ messages

    @listen(MessageCreate)
    async def audit_on_message(self, event: MessageCreate):
        message = event.message
        guild_id = getattr(message, "_guild_id", None)
        if not guild_id:
            return
        settings = await self._settings(guild_id)
        if settings is None or not settings.active:
            return
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        if not core.should_store(
            settings,
            author_is_bot=bool(getattr(author, "bot", False)),
            is_webhook=bool(getattr(message, "webhook_id", None)),
            channel_id=getattr(message, "_channel_id", None),
            parent_channel_id=getattr(channel, "parent_id", None),
        ):
            return
        attachments = list(getattr(message, "attachments", None) or [])
        row = {
            "message_id": int(message.id),
            "guild_id": int(guild_id),
            "channel_id": int(message._channel_id),
            "author_id": int(author.id),
            "author_name": _display_name(author),
            "content": getattr(message, "content", None) or "",
            "attachments": [{
                "filename": getattr(a, "filename", None) or "file",
                "content_type": getattr(a, "content_type", None),
                "size": getattr(a, "size", None),
                "url": str(getattr(a, "url", "") or ""),
                "path": None,
            } for a in attachments],
            "created_at": _utc_naive(_plain_datetime(getattr(message, "timestamp", None))) or datetime.now(timezone.utc),
            "edited_at": None,
        }
        self._remember(row)
        if attachments:
            await self._copy_images(row, attachments)
        # An edit during the image copy replaced the remembered row; write that one.
        self._pending_upserts.append(self._recent.get(row["message_id"], row))

    @listen(MessageUpdate)
    async def audit_on_edit(self, event: MessageUpdate):
        after = getattr(event, "after", None)
        if after is None:
            return
        guild_id = getattr(after, "_guild_id", None)
        if not guild_id:
            return
        settings = await self._settings(guild_id)
        if settings is None or not settings.active:
            return
        author = getattr(after, "author", None)
        channel = getattr(after, "channel", None)
        if not core.should_store(
            settings,
            author_is_bot=bool(getattr(author, "bot", False)),
            is_webhook=bool(getattr(after, "webhook_id", None)),
            channel_id=getattr(after, "_channel_id", None),
            parent_channel_id=getattr(channel, "parent_id", None),
        ):
            return
        # Link previews and other Discord-side updates also arrive as edits;
        # only a set edited_timestamp means the author changed something.
        edited = getattr(after, "edited_timestamp", None)
        if not edited:
            return
        new_content = getattr(after, "content", None) or ""
        message_id = int(after.id)
        stored = (await self._lookup([message_id])).get(message_id)
        if stored is not None and (stored.get("content") or "") == new_content:
            return
        if settings.logs("message_edit"):
            embed = core.message_edit_embed(
                stored, guild_id=guild_id, channel_id=after._channel_id, message_id=message_id,
                author_id=author.id, author_name=_display_name(author), after=new_content)
            self._queue_post(settings, embed)
        row = dict(stored) if stored else {
            "message_id": message_id,
            "guild_id": int(guild_id),
            "channel_id": int(after._channel_id),
            "author_id": int(author.id),
            "author_name": _display_name(author),
            "attachments": [],
            "created_at": _utc_naive(_plain_datetime(getattr(after, "timestamp", None))),
        }
        row["content"] = new_content
        row["edited_at"] = _utc_naive(_plain_datetime(edited)) or datetime.now(timezone.utc)
        self._remember(row)
        self._pending_upserts.append(row)

    @listen("raw_message_delete")
    async def audit_on_delete(self, event: RawGatewayEvent):
        data = event.data or {}
        guild_id = data.get("guild_id")
        message_id = data.get("id")
        if not guild_id or not message_id:
            return
        settings = await self._settings(guild_id)
        if settings is None or not settings.active:
            return
        row = (await self._lookup([message_id])).get(int(message_id))
        if row is None:
            return  # not a stored message: a bot's, or from before logging
        self._pending_deleted.append(int(message_id))
        if not settings.logs("message_delete"):
            return
        deleted_by, known = await self._who_deleted(guild_id, row["author_id"], row["channel_id"])
        embed = core.message_delete_embed(row, deleted_by=deleted_by, attribution_known=known)
        files = await asyncio.to_thread(self._image_files, row)
        self._queue_post(settings, embed, files)

    async def _who_deleted(self, guild_id, author_id, channel_id) -> tuple:
        """(moderator id or None, whether the audit log could be read)."""
        await asyncio.sleep(ATTRIBUTION_DELAY_SECONDS)
        key = str(guild_id)
        lock = self._attribution_locks.setdefault(key, asyncio.Lock())
        async with lock:
            tracker = self._attribution.setdefault(key, core.DeleteAttribution())
            try:
                log = await self.bot.http.get_audit_log(
                    int(guild_id), action_type=core.AUDIT_MESSAGE_DELETE, limit=25)
            except Exception as e:  # noqa: BLE001
                _log(f"audit log read failed in {key} (needs View Audit Log): {e}")
                return None, False
            tracker.absorb((log or {}).get("audit_log_entries") or [])
            return tracker.take(author_id, channel_id), True

    @listen("raw_message_delete_bulk")
    async def audit_on_bulk_delete(self, event: RawGatewayEvent):
        data = event.data or {}
        guild_id = data.get("guild_id")
        ids = data.get("ids") or []
        channel_id = data.get("channel_id")
        if not guild_id or not ids:
            return
        settings = await self._settings(guild_id)
        if settings is None or not settings.active:
            return
        if core.is_ignored_channel(settings, channel_id):
            return
        rows = await self._lookup(ids)
        self._pending_deleted.extend(rows.keys())
        if not settings.logs("message_bulk_delete"):
            return
        deleted_by = None
        await asyncio.sleep(ATTRIBUTION_DELAY_SECONDS)
        try:
            log = await self.bot.http.get_audit_log(int(guild_id), action_type=73, limit=5)
            now_ms = time.time() * 1000
            for entry in (log or {}).get("audit_log_entries") or []:
                if (str(entry.get("target_id")) == str(channel_id)
                        and now_ms - core.snowflake_ms(entry["id"]) < 60_000):
                    deleted_by = entry.get("user_id")
                    break
        except Exception as e:  # noqa: BLE001
            _log(f"bulk delete audit read failed in {guild_id}: {e}")
        embed = core.bulk_delete_embed(channel_id=channel_id, total=len(ids), recorded=len(rows),
                                       deleted_by=deleted_by)
        files = []
        if rows:
            channel = self.bot.get_channel(int(channel_id)) if channel_id else None
            transcript = core.bulk_delete_transcript(list(rows.values()), getattr(channel, "name", None))
            files.append((f"bulk-delete-{channel_id}.txt", transcript.encode("utf-8")))
        self._queue_post(settings, embed, files)

    # ------------------------------------------------------------------ members

    @listen(MemberAdd)
    async def audit_on_join(self, event: MemberAdd):
        settings = await self._settings(getattr(event, "guild_id", None))
        if settings is None or not settings.logs("member_join"):
            return
        member = event.member
        self._queue_post(settings, core.member_join_embed(user_id=member.id, username=_display_name(member)))

    @listen(MemberRemove)
    async def audit_on_leave(self, event: MemberRemove):
        guild_id = getattr(event, "guild_id", None)
        settings = await self._settings(guild_id)
        if settings is None or not settings.logs("member_leave"):
            return
        member = event.member
        await asyncio.sleep(LEAVE_GRACE_SECONDS)
        acted = self._recent_mod_actions.get((str(guild_id), str(member.id)))
        if acted and time.time() - acted < 30:
            return  # the kick/ban post covers it
        joined_at = _utc_naive(_plain_datetime(getattr(member, "joined_at", None)))
        self._queue_post(settings, core.member_leave_embed(
            user_id=member.id, username=_display_name(member), joined_at=joined_at))

    @listen("raw_guild_audit_log_entry_create")
    async def audit_on_audit_entry(self, event: RawGatewayEvent):
        entry = event.data or {}
        guild_id = entry.get("guild_id")
        settings = await self._settings(guild_id)
        if settings is None or not settings.active:
            return
        try:
            action = int(entry.get("action_type") or 0)
        except (TypeError, ValueError):
            return
        target_id = entry.get("target_id")
        actor_id = entry.get("user_id")
        reason = entry.get("reason")
        mod_kinds = {core.AUDIT_MEMBER_KICK: "member_kick",
                     core.AUDIT_MEMBER_BAN_ADD: "member_ban",
                     core.AUDIT_MEMBER_BAN_REMOVE: "member_unban"}
        if action in mod_kinds:
            kind = mod_kinds[action]
            if kind in ("member_kick", "member_ban"):
                self._recent_mod_actions[(str(guild_id), str(target_id))] = time.time()
                self._trim_mod_actions()
            if settings.logs(kind):
                target = await self._user(target_id)
                self._queue_post(settings, core.moderation_embed(
                    kind, target_id=target_id, actor_id=actor_id, reason=reason,
                    target_name=_display_name(target)))
            return
        if action not in (core.AUDIT_MEMBER_UPDATE, core.AUDIT_MEMBER_ROLE_UPDATE):
            return
        described = [(k, d) for k, d in core.describe_member_update(entry) if settings.logs(k)]
        if not described:
            return
        actor = await self._user(actor_id)
        actor_is_bot = bool(getattr(actor, "bot", False))
        for kind, details in described:
            # Our own bots set nicknames and sync roles all day; only people's
            # changes are worth an audit post. Bot timeouts still post.
            if actor_is_bot and kind in ("member_nickname", "member_roles"):
                continue
            self._queue_post(settings, core.member_update_embed(
                kind, details, target_id=target_id, actor_id=actor_id, reason=reason))

    async def _user(self, user_id):
        if not user_id:
            return None
        try:
            return await self.bot.fetch_user(int(user_id))
        except Exception:  # noqa: BLE001
            return None

    def _trim_mod_actions(self) -> None:
        if len(self._recent_mod_actions) > 1000:
            cutoff = time.time() - 60
            self._recent_mod_actions = {k: v for k, v in self._recent_mod_actions.items() if v > cutoff}

    # ------------------------------------------------------------------ commands

    @staticmethod
    def _can_manage(ctx: SlashContext) -> bool:
        author = ctx.author
        try:
            return bool(author.has_permission(Permissions.MANAGE_GUILD))
        except Exception:  # noqa: BLE001
            return False

    async def _guard(self, ctx: SlashContext) -> bool:
        if ctx.guild_id is None:
            await ctx.send("Use this in a server.", ephemeral=True)
            return False
        if not self._can_manage(ctx):
            await ctx.send("You need the Manage Server permission to change the audit log.", ephemeral=True)
            return False
        return True

    @slash_command(
        name="auditlog",
        description="Server audit log",
        sub_cmd_name="channel",
        sub_cmd_description="Turn the audit log on and choose its (private) channel",
        scopes=COMMAND_SCOPES,
        default_member_permissions=Permissions.MANAGE_GUILD,
        dm_permission=False,
    )
    @slash_option(name="channel", description="An admin-only text channel",
                  opt_type=OptionType.CHANNEL, required=True, channel_types=[ChannelType.GUILD_TEXT])
    async def auditlog_channel(self, ctx: SlashContext, channel):
        if not await self._guard(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            everyone = ctx.guild.default_role
            if Permissions.VIEW_CHANNEL in channel.permissions_for(everyone):
                return await ctx.send(
                    f"{channel.mention} can be seen by everyone. Audit posts include deleted messages, "
                    "so pick a channel only admins can see.", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            _log(f"privacy check failed for {channel.id}: {e}")
        try:
            await channel.send(embeds=[{
                "title": "Audit log on",
                "description": "Deleted and edited messages, joins, leaves and moderation actions "
                               "will be posted here.",
                "color": core.COLOR_INFO,
            }], allowed_mentions=_NO_PINGS)
        except Exception as e:  # noqa: BLE001
            return await ctx.send(f"I can't post in {channel.mention} ({e}). Give me View Channel, "
                                  "Send Messages, Embed Links and Attach Files there.", ephemeral=True)
        settings = await asyncio.to_thread(_save_settings, str(ctx.guild_id), str(ctx.author.id),
                                           enabled=True, channel_id=str(channel.id))
        self._remember_settings(settings)
        self._warned_channels.discard(str(channel.id))
        note = ""
        try:
            me = ctx.guild.me
            if not me.has_permission(Permissions.VIEW_AUDIT_LOG):
                note = ("\nI don't have **View Audit Log**, so I can't tell who deleted a message "
                        "or who kicked, banned or timed someone out.")
        except Exception:  # noqa: BLE001
            pass
        await ctx.send(f"The audit log is on and posting to {channel.mention}.{note}", ephemeral=True)

    @slash_command(
        name="auditlog",
        description="Server audit log",
        sub_cmd_name="off",
        sub_cmd_description="Pause the audit log (settings are kept)",
        scopes=COMMAND_SCOPES,
        default_member_permissions=Permissions.MANAGE_GUILD,
        dm_permission=False,
    )
    async def auditlog_off(self, ctx: SlashContext):
        if not await self._guard(ctx):
            return
        settings = await asyncio.to_thread(_save_settings, str(ctx.guild_id), str(ctx.author.id), enabled=False)
        self._remember_settings(settings)
        await ctx.send("The audit log is paused. New messages are no longer stored. "
                       "Turn it back on with `/auditlog channel`.", ephemeral=True)

    @slash_command(
        name="auditlog",
        description="Server audit log",
        sub_cmd_name="status",
        sub_cmd_description="Show the audit log settings",
        scopes=COMMAND_SCOPES,
        default_member_permissions=Permissions.MANAGE_GUILD,
        dm_permission=False,
    )
    async def auditlog_status(self, ctx: SlashContext):
        if not await self._guard(ctx):
            return
        await ctx.defer(ephemeral=True)
        settings = await asyncio.to_thread(_load_settings, str(ctx.guild_id))
        stored = await asyncio.to_thread(_stored_count, str(ctx.guild_id)) if settings else None
        await ctx.send(core.status_text(settings, stored), ephemeral=True, allowed_mentions=_NO_PINGS)

    @slash_command(
        name="auditlog",
        description="Server audit log",
        sub_cmd_name="event",
        sub_cmd_description="Turn one kind of audit post on or off",
        scopes=COMMAND_SCOPES,
        default_member_permissions=Permissions.MANAGE_GUILD,
        dm_permission=False,
    )
    @slash_option(name="kind", description="Which posts", opt_type=OptionType.STRING, required=True,
                  choices=[SlashCommandChoice(name=label, value=key) for key, label in core.EVENT_LABELS.items()])
    @slash_option(name="on", description="On or off", opt_type=OptionType.BOOLEAN, required=True)
    async def auditlog_event(self, ctx: SlashContext, kind: str, on: bool):
        if not await self._guard(ctx):
            return
        current = await asyncio.to_thread(_load_settings, str(ctx.guild_id))
        overrides = {k: v for k, v in (current.events if current else {}).items()
                     if v != core.DEFAULT_EVENTS.get(k)}
        settings = await asyncio.to_thread(_save_settings, str(ctx.guild_id), str(ctx.author.id),
                                           events=core.set_event(overrides, kind, on))
        self._remember_settings(settings)
        await ctx.send(f"{core.EVENT_LABELS[kind]}: **{'on' if on else 'off'}**.", ephemeral=True)

    @slash_command(
        name="auditlog",
        description="Server audit log",
        sub_cmd_name="ignore",
        sub_cmd_description="Stop or start logging messages from a channel",
        scopes=COMMAND_SCOPES,
        default_member_permissions=Permissions.MANAGE_GUILD,
        dm_permission=False,
    )
    @slash_option(name="channel", description="The channel (its threads follow it)",
                  opt_type=OptionType.CHANNEL, required=True)
    async def auditlog_ignore(self, ctx: SlashContext, channel):
        if not await self._guard(ctx):
            return
        current = await asyncio.to_thread(_load_settings, str(ctx.guild_id))
        ignored = set(current.ignored_channel_ids) if current else set()
        cid = str(channel.id)
        if cid in ignored:
            ignored.discard(cid)
            msg = f"Messages in {channel.mention} are logged again."
        else:
            ignored.add(cid)
            msg = (f"Messages in {channel.mention} are no longer stored or logged. "
                   "Copies already stored stay until they expire.")
        settings = await asyncio.to_thread(_save_settings, str(ctx.guild_id), str(ctx.author.id),
                                           ignored_channel_ids=sorted(ignored))
        self._remember_settings(settings)
        await ctx.send(msg, ephemeral=True)

    @slash_command(
        name="auditlog",
        description="Server audit log",
        sub_cmd_name="retention",
        sub_cmd_description="How many days message copies are kept",
        scopes=COMMAND_SCOPES,
        default_member_permissions=Permissions.MANAGE_GUILD,
        dm_permission=False,
    )
    @slash_option(name="days", description="1 to 90", opt_type=OptionType.INTEGER, required=True,
                  min_value=1, max_value=90)
    async def auditlog_retention(self, ctx: SlashContext, days: int):
        if not await self._guard(ctx):
            return
        settings = await asyncio.to_thread(_save_settings, str(ctx.guild_id), str(ctx.author.id),
                                           retention_days=int(days))
        self._remember_settings(settings)
        await ctx.send(f"Message copies are now kept for {days} days. Older ones are removed "
                       "within the hour.", ephemeral=True)

    @slash_command(
        name="auditlog",
        description="Server audit log",
        sub_cmd_name="forget",
        sub_cmd_description="Delete every stored message from one person",
        scopes=COMMAND_SCOPES,
        default_member_permissions=Permissions.MANAGE_GUILD,
        dm_permission=False,
    )
    @slash_option(name="user", description="Whose stored messages to delete",
                  opt_type=OptionType.USER, required=True)
    async def auditlog_forget(self, ctx: SlashContext, user):
        if not await self._guard(ctx):
            return
        await ctx.defer(ephemeral=True)
        # Drop in-memory copies too, so a delete right now can't resurface them.
        for mid in [m for m, r in self._recent.items()
                    if str(r.get("author_id")) == str(user.id) and str(r.get("guild_id")) == str(ctx.guild_id)]:
            self._recent.pop(mid, None)
        self._pending_upserts = [r for r in self._pending_upserts
                                 if not (str(r.get("author_id")) == str(user.id)
                                         and str(r.get("guild_id")) == str(ctx.guild_id))]
        count = await asyncio.to_thread(_forget_author, str(ctx.guild_id), str(user.id))
        await ctx.send(f"Deleted {count:,} stored message(s) from <@{user.id}>.", ephemeral=True,
                       allowed_mentions=_NO_PINGS)


def _write_file(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def setup(bot):
    DiscordAudit(bot)
