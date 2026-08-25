"""Discord → web mirror for the suggestion forum (webhook bot extension).

Watches the suggestions/bugs forum channels (SUGGESTIONS_FORUM_CHANNEL_ID /
BUGS_FORUM_CHANNEL_ID) and keeps the ``suggestions`` / ``suggestion_messages``
tables in lockstep so the website forum shows the same threads and replies:

  * human messages in tracked forum posts are mirrored as replies
  * posts created directly in Discord become web threads (origin='discord')
  * message edits/deletes and thread archive/lock/delete state are synced
  * a startup backfill imports existing posts and heals downtime gaps

The web → Discord direction is NOT here: the Web API enqueues discord_outbox
rows that the core bot relays (services/discord_outbox.py). Every message the
mirror would see from that path is bot-authored, and bot authors are skipped —
that asymmetry is what prevents echo loops.
"""
import asyncio
import os
from datetime import datetime
from typing import Optional

from interactions import Extension, listen
from interactions.api.events import (
    MessageCreate,
    MessageDelete,
    MessageUpdate,
    Startup,
    ThreadDelete,
    ThreadUpdate,
)

from db.models import Session, Suggestion, SuggestionMessage, User


def _forum_type_map() -> dict[str, str]:
    """channel_id (str) -> suggestion type, from env (unset entries skipped)."""
    mapping = {}
    for env_key, kind in (
        ("SUGGESTIONS_FORUM_CHANNEL_ID", "suggestion"),
        ("BUGS_FORUM_CHANNEL_ID", "bug"),
    ):
        value = (os.getenv(env_key) or "").strip()
        if value:
            mapping[value] = kind
    return mapping


# Mirrors ticket_transcripts._plain_datetime: interactions' Timestamp
# subclasses datetime, and PyMySQL only type-maps exact datetime.
def _plain_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return datetime.fromtimestamp(value.timestamp())
    return datetime.now()


def _resolve_author_user_id(session, discord_id: str) -> Optional[int]:
    row = session.query(User.user_id).filter(User.discord_id == str(discord_id)).first()
    return row[0] if row else None


def _display_name(author) -> str:
    return str(
        getattr(author, "display_name", None)
        or getattr(author, "username", None)
        or getattr(author, "id", "unknown")
    )[:100]


class SuggestionSync(Extension):
    def __init__(self, bot):
        self.forum_types = _forum_type_map()
        self._started = False
        # Loaded from webhook_bot's own Startup handler, so a @listen(Startup)
        # here can register after the event fired — defer via a ready-wait
        # task instead (same pattern as the Tickets extension).
        asyncio.create_task(self._deferred_start())

    async def _deferred_start(self):
        try:
            await self.bot.wait_until_ready()
        except Exception as e:
            print(f"[suggestion_sync] wait_until_ready failed: {e}")
        if self._started:
            return
        self._started = True
        if not self.forum_types:
            print("[suggestion_sync] no forum channels configured; mirror idle")
            return
        print(f"[suggestion_sync] mirroring forums: {self.forum_types}")
        asyncio.create_task(self._backfill())

    @listen(Startup)
    async def _sync_startup(self, event: Startup):
        await self._deferred_start()

    # ── helpers ──────────────────────────────────────────────────────────

    def _tracked_parent(self, channel) -> Optional[str]:
        """The suggestion type when ``channel`` is a post in a tracked forum."""
        parent_id = getattr(channel, "parent_id", None)
        return self.forum_types.get(str(parent_id)) if parent_id else None

    async def _starter_is_human(self, thread) -> Optional[bool]:
        """Whether the post's starter message has a human author. In forum
        posts the starter message id equals the thread id. None = unknown."""
        try:
            starter = await thread.fetch_message(thread.id)
        except Exception:
            return None
        if starter is None:
            return None
        author = starter.author
        return not (getattr(author, "bot", False) or getattr(author, "system", False))

    def _ensure_thread(self, s, thread, kind: str, starter_message=None) -> Optional[Suggestion]:
        """Fetch-or-create the Suggestion row for a Discord forum post.

        Only creates rows for human-started posts: bot-started posts are the
        web's own syndicated threads, which already exist (the outbox drain
        writes back their thread id).
        """
        sug = (
            s.query(Suggestion)
            .filter(Suggestion.discord_thread_id == str(thread.id))
            .first()
        )
        if sug is not None:
            return sug
        if starter_message is None:
            return None
        author = starter_message.author
        if getattr(author, "bot", False) or getattr(author, "system", False):
            return None
        created = _plain_datetime(getattr(starter_message, "created_at", None))
        sug = Suggestion(
            user_id=_resolve_author_user_id(s, str(author.id)),
            origin="discord",
            author_discord_id=str(author.id),
            author_name=_display_name(author),
            type=kind,
            title=str(getattr(thread, "name", "") or "Untitled")[:100],
            body_md=(starter_message.content or "").strip() or "*(no text content)*",
            status="posted",
            discord_thread_id=str(thread.id),
            created_at=created,
            last_activity_at=created,
        )
        s.add(sug)
        s.flush()
        return sug

    def _upsert_reply(self, s, sug: Suggestion, message) -> bool:
        """Idempotently mirror one human Discord message as a reply.
        Returns True when newly inserted."""
        existing = (
            s.query(SuggestionMessage)
            .filter(SuggestionMessage.discord_message_id == str(message.id))
            .first()
        )
        content = (message.content or "").strip()
        if existing is not None:
            if content and (existing.content or "") != content:
                existing.content = content
                existing.edited_at = datetime.now()
            return False
        if not content:
            return False  # attachment/embed-only messages have no mirrorable text
        author = message.author
        created = _plain_datetime(getattr(message, "created_at", None))
        row = SuggestionMessage(
            suggestion_id=sug.id,
            author_user_id=_resolve_author_user_id(s, str(author.id)),
            author_discord_id=str(author.id),
            author_name=_display_name(author),
            source="discord",
            content=content,
            discord_message_id=str(message.id),
            created_at=created,
        )
        s.add(row)
        sug.message_count = int(sug.message_count or 0) + 1
        if sug.last_activity_at is None or created > sug.last_activity_at:
            sug.last_activity_at = created
        s.flush()
        if row.author_user_id is not None:
            # A Discord-side reply proves its author is caught up — advance
            # their site inbox pointer so no badge appears (web102a).
            from services.inbox import advance_own_reply

            advance_own_reply(s, "suggestion", sug.id, row.author_user_id, row.id)
        return True

    # ── live listeners ───────────────────────────────────────────────────

    @listen(MessageCreate)
    async def on_forum_message(self, event: MessageCreate):
        message = event.message
        author = message.author
        if getattr(author, "bot", False) or getattr(author, "system", False):
            return
        kind = self._tracked_parent(message.channel)
        if kind is None:
            return
        thread = message.channel

        def _mirror():
            s = Session()
            try:
                sug = self._ensure_thread(s, thread, kind, starter_message=message)
                if sug is None:
                    # Reply in a web-origin thread whose id write-back hasn't
                    # landed yet (≤10s window); the startup backfill heals it.
                    return
                inserted = False
                if str(message.id) != str(thread.id):  # starter is the body
                    inserted = self._upsert_reply(s, sug, message)
                s.commit()
                if inserted:
                    # Bodyless badge poke for the site inbox (web102a).
                    from services.inbox import (
                        publish_inbox_unread,
                        suggestion_participant_user_ids,
                    )

                    author_uid = _resolve_author_user_id(s, str(author.id))
                    publish_inbox_unread(
                        "suggestion",
                        sug.id,
                        suggestion_participant_user_ids(s, sug),
                        exclude_user_id=author_uid,
                    )
            except Exception as e:  # noqa: BLE001
                s.rollback()
                print(f"[suggestion_sync] mirror failed (thread {thread.id}): {e}")
            finally:
                s.close()

        await asyncio.to_thread(_mirror)

    @listen(MessageUpdate)
    async def on_forum_message_edit(self, event: MessageUpdate):
        message = event.after
        if message is None:
            return
        author = message.author
        if getattr(author, "bot", False) or getattr(author, "system", False):
            return
        kind = self._tracked_parent(message.channel)
        if kind is None:
            return
        thread = message.channel

        def _apply():
            s = Session()
            try:
                content = (message.content or "").strip()
                if str(message.id) == str(thread.id):
                    # Starter edit = thread body edit.
                    sug = (
                        s.query(Suggestion)
                        .filter(Suggestion.discord_thread_id == str(thread.id))
                        .first()
                    )
                    if sug is not None and content and sug.body_md != content:
                        sug.body_md = content
                else:
                    row = (
                        s.query(SuggestionMessage)
                        .filter(SuggestionMessage.discord_message_id == str(message.id))
                        .first()
                    )
                    if row is not None and content and (row.content or "") != content:
                        row.content = content
                        row.edited_at = datetime.now()
                s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                print(f"[suggestion_sync] edit sync failed: {e}")
            finally:
                s.close()

        await asyncio.to_thread(_apply)

    @listen(MessageDelete)
    async def on_forum_message_delete(self, event: MessageDelete):
        message = getattr(event, "message", None)
        if message is None:
            return
        if self._tracked_parent(getattr(message, "channel", None)) is None:
            return

        def _apply():
            s = Session()
            try:
                row = (
                    s.query(SuggestionMessage)
                    .filter(SuggestionMessage.discord_message_id == str(message.id))
                    .first()
                )
                if row is not None:
                    sug = s.query(Suggestion).filter(Suggestion.id == row.suggestion_id).first()
                    if sug is not None and (sug.message_count or 0) > 0:
                        sug.message_count = sug.message_count - 1
                    s.delete(row)
                    s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                print(f"[suggestion_sync] delete sync failed: {e}")
            finally:
                s.close()

        await asyncio.to_thread(_apply)

    @listen(ThreadUpdate)
    async def on_thread_update(self, event: ThreadUpdate):
        thread = event.thread
        if self._tracked_parent(thread) is None:
            return
        archived = bool(getattr(thread, "archived", False))
        locked = bool(getattr(thread, "locked", False))
        await asyncio.to_thread(self._set_open, str(thread.id), not (archived or locked))

    @listen(ThreadDelete)
    async def on_thread_delete(self, event: ThreadDelete):
        thread = event.thread
        if self._tracked_parent(thread) is None:
            return
        await asyncio.to_thread(self._set_open, str(thread.id), False)

    def _set_open(self, thread_id: str, is_open: bool):
        s = Session()
        try:
            sug = (
                s.query(Suggestion)
                .filter(Suggestion.discord_thread_id == thread_id)
                .first()
            )
            if sug is not None and bool(sug.is_open) != is_open:
                sug.is_open = is_open
                s.commit()
        except Exception as e:  # noqa: BLE001
            s.rollback()
            print(f"[suggestion_sync] open-state sync failed: {e}")
        finally:
            s.close()

    # ── startup backfill ─────────────────────────────────────────────────

    async def _backfill(self):
        """Import/heal every post in the tracked forums (idempotent). Covers
        posts and replies made while the bots were down, and the initial
        import of forums that predate this feature."""
        imported = 0
        for channel_id, kind in self.forum_types.items():
            try:
                forum = await self.bot.fetch_channel(int(channel_id))
                posts = list(await forum.fetch_posts())
                async for post in forum.archived_posts(limit=100):
                    posts.append(post)
            except Exception as e:  # noqa: BLE001
                print(f"[suggestion_sync] backfill: forum {channel_id} unavailable: {e}")
                continue
            for post in posts:
                try:
                    if await self._backfill_post(post, kind):
                        imported += 1
                except Exception as e:  # noqa: BLE001
                    print(f"[suggestion_sync] backfill failed for post {post.id}: {e}")
                await asyncio.sleep(0.5)  # be gentle with the API on startup
        print(f"[suggestion_sync] backfill complete; {imported} posts synced")

    async def _backfill_post(self, post, kind: str) -> bool:
        starter = None
        try:
            starter = await post.fetch_message(post.id)
        except Exception:
            pass  # starter deleted; thread may still hold replies to a web row
        messages = []
        try:
            async for m in post.history(limit=200):
                messages.append(m)
        except Exception as e:  # noqa: BLE001
            print(f"[suggestion_sync] history fetch failed for post {post.id}: {e}")
        messages.reverse()  # history yields newest-first

        def _apply() -> bool:
            s = Session()
            try:
                sug = self._ensure_thread(s, post, kind, starter_message=starter)
                if sug is None:
                    return False
                for m in messages:
                    author = m.author
                    if getattr(author, "bot", False) or getattr(author, "system", False):
                        continue
                    if str(m.id) == str(post.id):
                        continue  # starter lives in body_md
                    self._upsert_reply(s, sug, m)
                archived = bool(getattr(post, "archived", False))
                locked = bool(getattr(post, "locked", False))
                sug.is_open = not (archived or locked)
                s.commit()
                return True
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        return await asyncio.to_thread(_apply)


def setup(bot):
    SuggestionSync(bot)
