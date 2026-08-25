"""Suggestion forum: web threads mirrored two-way with Discord forum posts.

  GET  /api/v1/suggestions               -> public thread index (filters below)
  GET  /api/v1/suggestions/{id}          -> thread detail + replies (public)
  POST /api/v1/suggestions               -> create a thread (authed)
  POST /api/v1/suggestions/{id}/messages -> reply on a thread (authed)

Threads and replies flow both ways. Web-created threads/replies are stored
here and enqueued on the discord_outbox (``forum_post`` / ``message`` rows)
for the core bot to relay — the Web API never talks to Discord (§10.2).
Discord-side activity is mirrored back into these tables by the webhook
bot (services/suggestion_sync.py), so both surfaces show the same forum.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Optional

from quart import Blueprint, jsonify, request

from db.models import Suggestion, SuggestionMessage, User
from utils.redis import redis_client
from web_api.common import abort_problem, db_session, parse_page, private_no_store
from web_api.deps import current_user_id, json_body, load_user, optional_user_id
from web_api.mentions import clean_tokens, resolve_user_mentions

suggestions_bp = Blueprint("v1_suggestions", __name__)

_TYPES = ("suggestion", "bug")
_TITLE_MIN, _TITLE_MAX = 5, 100          # thread names cap at 100 on Discord
_BODY_MIN, _BODY_MAX = 20, 4000
_REPLY_MIN, _REPLY_MAX = 2, 1800
# Discord message content caps at 2000; leave room for the attribution footer.
_DISCORD_BODY_MAX = 1700

_PRIMARY_GUILD_ID = os.getenv("PRIMARY_GUILD_ID", "1172737525069135962").strip()

# Per-user rate limits (Redis, 10-minute windows).
_THREAD_LIMIT = int(os.getenv("WEB_SUGGESTIONS_PER_10MIN", "5"))
_REPLY_LIMIT = int(os.getenv("WEB_SUGGESTION_REPLIES_PER_10MIN", "15"))

_MD_STRIP_RE = re.compile(r"[#*_`>~\[\]()|-]+")


def _forum_channel_id(kind: str) -> Optional[str]:
    key = "BUGS_FORUM_CHANNEL_ID" if kind == "bug" else "SUGGESTIONS_FORUM_CHANNEL_ID"
    value = (os.getenv(key) or "").strip()
    return value or None


def _rc():
    return getattr(redis_client, "client", None)


def _rate_limited(bucket: str, user_id: int, limit: int) -> bool:
    conn = _rc()
    if conn is None:
        return False
    try:
        key = f"web:ratelimit:{bucket}:{user_id}"
        count = conn.incr(key)
        if count == 1:
            conn.expire(key, 600)
        return count > limit
    except Exception:
        return False


def _discord_content(kind: str, body_md: str, discord_id: Optional[str], suggestion_id: int) -> str:
    """Starter-message content for the forum post: the Markdown body followed
    by a small attribution footer that pings the author."""
    body = body_md.strip()
    if len(body) > _DISCORD_BODY_MAX:
        body = body[:_DISCORD_BODY_MAX].rstrip() + "\n\n*…(truncated — full text on the website)*"
    label = "Bug report" if kind == "bug" else "Suggestion"
    author = f"<@{discord_id}>" if discord_id else "a DropTracker user"
    footer = (
        f"-# {label} `#{suggestion_id}` · submitted by {author} via "
        f"[droptracker.io](https://www.droptracker.io/suggestions)"
    )
    return f"{body}\n\n{footer}"


def _discord_reply_content(content: str, discord_id: Optional[str], author_name: str) -> str:
    """Relay content for a web reply, attributed via a subtext footer."""
    body = content.strip()
    if len(body) > _REPLY_MAX:
        body = body[:_REPLY_MAX].rstrip() + "…"
    author = f"<@{discord_id}>" if discord_id else f"**{author_name}**"
    return f"{body}\n\n-# \N{SPEECH BALLOON} {author} replied via [droptracker.io](https://www.droptracker.io/suggestions)"


def _thread_url(thread_id: Optional[str]) -> Optional[str]:
    if not thread_id:
        return None
    return f"https://discord.com/channels/{_PRIMARY_GUILD_ID}/{thread_id}"


def _excerpt(body_md: str, user_map: Optional[dict] = None, limit: int = 160) -> str:
    # Resolve Discord tokens (e.g. <@123>) to readable text *before* stripping
    # Markdown, so previews don't show raw mention ids.
    text = clean_tokens(body_md, user_map or {})
    text = " ".join(_MD_STRIP_RE.sub(" ", text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _author_name(s, obj) -> str:
    """Display name: live site username when linked, else the Discord
    snapshot taken at mirror time."""
    uid = obj.author_user_id if isinstance(obj, SuggestionMessage) else obj.user_id
    if uid:
        row = s.query(User.username).filter(User.user_id == uid).first()
        if row and row[0]:
            return row[0]
    return obj.author_name or "Unknown"


def _ts(dt: Optional[datetime]) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def _summary(s, sug: Suggestion, user_map: Optional[dict] = None) -> dict:
    return {
        "id": sug.id,
        "type": sug.type,
        "title": sug.title,
        "status": sug.status,
        "origin": sug.origin,
        "is_open": bool(sug.is_open),
        "author_name": _author_name(s, sug),
        "author_user_id": sug.user_id,
        "excerpt": _excerpt(sug.body_md, user_map),
        "message_count": int(sug.message_count or 0),
        "discord_thread_url": _thread_url(sug.discord_thread_id),
        "created_at": _ts(sug.created_at),
        "last_activity_at": _ts(sug.last_activity_at),
    }


def _message_row(s, m: SuggestionMessage) -> dict:
    return {
        "id": m.id,
        "author_name": _author_name(s, m),
        "author_user_id": m.author_user_id,
        "source": m.source,
        "content": m.content,
        "created_at": _ts(m.created_at),
        "edited_at": _ts(m.edited_at),
    }


@suggestions_bp.get("/suggestions")
async def list_suggestions():
    page, limit = parse_page(request, default_limit=25, max_limit=100)
    kind = (request.args.get("type") or "").strip().lower()
    mine = (request.args.get("mine") or "").strip() in ("1", "true")
    open_only = (request.args.get("open") or "").strip() in ("1", "true")
    user_id = current_user_id() if mine else None  # aborts 401 when mine w/o session

    def _load():
        with db_session() as s:
            query = s.query(Suggestion)
            if kind in _TYPES:
                query = query.filter(Suggestion.type == kind)
            if mine:
                query = query.filter(Suggestion.user_id == user_id)
            if open_only:
                query = query.filter(Suggestion.is_open.is_(True))
            query = query.order_by(
                Suggestion.last_activity_at.desc(), Suggestion.id.desc()
            )
            total = query.count()
            rows = query.offset((page - 1) * limit).limit(limit).all()
            user_map = resolve_user_mentions(s, (r.body_md for r in rows))
            return {
                "items": [_summary(s, r, user_map) for r in rows],
                "meta": {"page": page, "limit": limit, "total": int(total)},
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@suggestions_bp.get("/suggestions/<int:suggestion_id>")
async def suggestion_detail(suggestion_id: int):
    def _load():
        with db_session() as s:
            sug = s.query(Suggestion).filter(Suggestion.id == suggestion_id).first()
            if sug is None:
                abort_problem(404, "Not found", "No such suggestion.")
            messages = (
                s.query(SuggestionMessage)
                .filter(SuggestionMessage.suggestion_id == suggestion_id)
                .order_by(SuggestionMessage.created_at.asc(), SuggestionMessage.id.asc())
                .all()
            )
            user_map = resolve_user_mentions(
                s, [sug.body_md, *(m.content for m in messages)]
            )
            payload = _summary(s, sug, user_map)
            payload["body_md"] = sug.body_md
            payload["messages"] = [_message_row(s, m) for m in messages]
            payload["mentions"] = user_map
            return payload

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@suggestions_bp.post("/suggestions")
async def create_suggestion():
    user_id = current_user_id()
    body = await json_body()

    kind = str(body.get("type") or "").strip().lower()
    if kind not in _TYPES:
        abort_problem(422, "Invalid type", f"'type' must be one of {list(_TYPES)}.")

    title = " ".join(str(body.get("title") or "").split())
    if not (_TITLE_MIN <= len(title) <= _TITLE_MAX):
        abort_problem(
            422, "Invalid title",
            f"'title' must be {_TITLE_MIN}-{_TITLE_MAX} characters.",
        )

    body_md = str(body.get("body_md") or "").strip()
    if not (_BODY_MIN <= len(body_md) <= _BODY_MAX):
        abort_problem(
            422, "Invalid body",
            f"'body_md' must be {_BODY_MIN}-{_BODY_MAX} characters.",
        )

    channel_id = _forum_channel_id(kind)
    if not channel_id:
        abort_problem(
            503, "Not configured",
            "The Discord forum channel for this submission type is not configured.",
        )

    if _rate_limited("suggestions", user_id, _THREAD_LIMIT):
        abort_problem(429, "Too many submissions", "Slow down and try again in a few minutes.")

    def _create():
        with db_session() as s:
            user = load_user(s, user_id)
            discord_id = getattr(user, "discord_id", None)
            sug = Suggestion(
                user_id=user_id,
                origin="web",
                author_discord_id=str(discord_id) if discord_id else None,
                author_name=getattr(user, "username", None),
                type=kind,
                title=title,
                body_md=body_md,
                last_activity_at=datetime.now(),
            )
            s.add(sug)
            s.flush()  # assign sug.id for the footer before enqueueing

            from services.discord_outbox import enqueue

            enqueue(
                s,
                channel_id=channel_id,
                content=_discord_content(kind, body_md, discord_id, sug.id),
                kind="forum_post",
                ref_type="suggestion",
                ref_id=sug.id,
                actor_user_id=user_id,
                commit=False,
            )
            s.commit()
            user_map = resolve_user_mentions(s, [sug.body_md])
            payload = _summary(s, sug, user_map)
            payload["body_md"] = sug.body_md
            payload["messages"] = []
            payload["mentions"] = user_map
            return payload

    payload = await asyncio.to_thread(_create)
    return private_no_store(jsonify(payload)), 201


@suggestions_bp.post("/suggestions/<int:suggestion_id>/messages")
async def create_suggestion_message(suggestion_id: int):
    user_id = current_user_id()
    body = await json_body()

    content = str(body.get("content") or "").strip()
    if not (_REPLY_MIN <= len(content) <= _REPLY_MAX):
        abort_problem(
            422, "Invalid content",
            f"'content' must be {_REPLY_MIN}-{_REPLY_MAX} characters.",
        )

    if _rate_limited("suggestion_replies", user_id, _REPLY_LIMIT):
        abort_problem(429, "Too many replies", "Slow down and try again in a few minutes.")

    def _create():
        with db_session() as s:
            sug = s.query(Suggestion).filter(Suggestion.id == suggestion_id).first()
            if sug is None:
                abort_problem(404, "Not found", "No such suggestion.")
            if not sug.is_open:
                abort_problem(409, "Thread closed", "This thread is closed on Discord.")

            user = load_user(s, user_id)
            discord_id = getattr(user, "discord_id", None)
            author_name = getattr(user, "username", None) or f"user {user_id}"
            msg = SuggestionMessage(
                suggestion_id=suggestion_id,
                author_user_id=user_id,
                author_discord_id=str(discord_id) if discord_id else None,
                author_name=author_name,
                source="web",
                content=content,
            )
            s.add(msg)
            sug.message_count = int(sug.message_count or 0) + 1
            sug.last_activity_at = datetime.now()
            s.flush()

            # Relay to the Discord thread once it exists; a still-pending
            # thread will carry the reply over when the mirror backfills.
            if sug.discord_thread_id:
                from services.discord_outbox import enqueue

                enqueue(
                    s,
                    channel_id=sug.discord_thread_id,
                    content=_discord_reply_content(content, discord_id, author_name),
                    kind="message",
                    ref_type="suggestion_message",
                    ref_id=msg.id,
                    actor_user_id=user_id,
                    commit=False,
                )
            # Posting implies reading (web102a).
            from services.inbox import advance_own_reply

            advance_own_reply(s, "suggestion", suggestion_id, user_id, msg.id)
            s.commit()
            try:
                from services.inbox import (
                    publish_inbox_unread,
                    suggestion_participant_user_ids,
                )

                publish_inbox_unread(
                    "suggestion",
                    suggestion_id,
                    suggestion_participant_user_ids(s, sug),
                    exclude_user_id=user_id,
                )
            except Exception:
                pass
            return _message_row(s, msg)

    payload = await asyncio.to_thread(_create)
    return private_no_store(jsonify(payload)), 201
