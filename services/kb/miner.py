"""Knowledgebase ingestion pipeline (kb01a).

Normalizes four content sources into ``kb_documents`` + ``kb_chunks`` so the
retriever can serve hybrid keyword+semantic search over them:

  * tickets  -- mirrored ticket transcripts (``ticket_messages``) -> one doc
                per ticket, chunked as a conversation.
  * docs     -- repo markdown (CLAUDE.md / README.md / CONTRIBUTING.md and the
                top level of ``docs/``) -> one doc per file, chunked by section.
  * forums   -- suggestion/bug forum threads over Discord REST -> one doc per
                thread, chunked as a conversation.
  * chat     -- general chat channels over Discord REST -> one doc per
                channel-month, chunked as a conversation and *appended*
                incrementally against a per-channel snowflake cursor.

Only ``kb_documents`` / ``kb_chunks`` / ``kb_ingest_state`` are written; every
other table is read-only. Each source has cheap change detection stored in the
document's ``meta_json`` (content hash for docs, max mirrored row id for
tickets, newest message id for forum threads, cursor state for chat) so re-runs
skip unchanged material.

Embedding is a separate, resumable pass (``embed_missing_chunks``): chunks are
written with ``embedding = NULL`` and filled in later, so ingestion never blocks
on the (optional) local embedding stack. The sync ingesters (docs, tickets) run
in-process; the Discord ingesters (forums, chat) are async, take a logged-in
``interactions`` client, and run every DB operation via ``asyncio.to_thread`` so
a session is never held across an ``await``.

DB session discipline: short-lived ``with Session() as s:`` blocks with an
explicit ``s.commit()``; never hold a session across ``await``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime

from sqlalchemy import func

from db.models import KBChunk, KBDocument, KBIngestState, Session, Ticket, TicketMessage
from services.kb import embedder
from services.kb.chunker import chunk_markdown, chunk_messages

# Repo root: .../services/kb/miner.py -> up three levels.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Guild the public forum threads live in (for building thread permalinks).
_GUILD_ID = "1172737525069135962"

# Top-level doc files always mined, plus docs/*.md (docs/archive/ excluded).
_TOP_DOC_FILES = ("CLAUDE.md", "README.md", "CONTRIBUTING.md")

# Chunks whose meaningful payload is shorter than this are dropped as noise.
_MIN_CHUNK_CHARS = 40
# Hard cap on stored content (TEXT column safety margin).
_MAX_CHUNK_CHARS = 60000


# ── helpers ───────────────────────────────────────────────────────────────


def _plain_datetime(value):
    """Coerce interactions' ``Timestamp`` (a ``datetime`` subclass PyMySQL
    mis-maps to its ``<t:...>`` string form) to an exact ``datetime``; pass any
    non-datetime (e.g. ``None``) through unchanged."""
    if isinstance(value, datetime):
        return datetime.fromtimestamp(value.timestamp())
    return value


def _sanitize(content: str) -> str:
    """Strip NUL bytes, which MySQL/the connector reject in TEXT columns."""
    return content.replace("\x00", "")


def _upsert_document(
    s,
    source_type: str,
    source_ref: str,
    *,
    title=None,
    url=None,
    author_label=None,
    started_at=None,
    meta: dict | None = None,
) -> KBDocument:
    """Get-or-create a ``KBDocument`` by ``(source_type, source_ref)`` and refresh
    its metadata. Flushes so ``doc.id`` is available to the caller."""
    ref = source_ref[:191]
    doc = (
        s.query(KBDocument)
        .filter(KBDocument.source_type == source_type, KBDocument.source_ref == ref)
        .first()
    )
    if doc is None:
        doc = KBDocument(source_type=source_type, source_ref=ref)
        s.add(doc)
    if title is not None:
        doc.title = title[:255]
    if url is not None:
        doc.url = url[:512]
    if author_label is not None:
        doc.author_label = author_label[:255]
    if started_at is not None:
        doc.started_at = started_at
    if meta is not None:
        doc.meta_json = json.dumps(meta)
    s.flush()
    return doc


def _prepare_chunk_contents(doc: KBDocument, texts: list[str]):
    """Yield storable chunk contents: title-prefixed, NUL-sanitized, length
    capped, with trivially-short payloads skipped."""
    prefix = f"[{doc.title}]\n" if doc.title else ""
    for text in texts:
        if len(text.strip()) < _MIN_CHUNK_CHARS:
            continue
        content = _sanitize(prefix + text)[:_MAX_CHUNK_CHARS]
        yield content


def _replace_chunks(s, doc: KBDocument, texts: list[str]) -> int:
    """Delete the document's existing chunks and insert ``texts`` as chunk 0..n-1.
    Embeddings are left NULL (filled by ``embed_missing_chunks``). Returns the
    number of chunks inserted."""
    s.query(KBChunk).filter(KBChunk.document_id == doc.id).delete(
        synchronize_session=False
    )
    inserted = 0
    for content in _prepare_chunk_contents(doc, texts):
        s.add(
            KBChunk(
                document_id=doc.id,
                chunk_index=inserted,
                content=content,
                token_estimate=len(content) // 4,
                embedding=None,
            )
        )
        inserted += 1
    return inserted


def _append_chunks(s, doc: KBDocument, texts: list[str]) -> int:
    """Insert ``texts`` after the document's existing chunks (no delete),
    continuing the ``chunk_index`` sequence. Returns the number inserted."""
    max_idx = (
        s.query(func.max(KBChunk.chunk_index))
        .filter(KBChunk.document_id == doc.id)
        .scalar()
    )
    start = (max_idx + 1) if max_idx is not None else 0
    inserted = 0
    for content in _prepare_chunk_contents(doc, texts):
        s.add(
            KBChunk(
                document_id=doc.id,
                chunk_index=start + inserted,
                content=content,
                token_estimate=len(content) // 4,
                embedding=None,
            )
        )
        inserted += 1
    return inserted


def _get_state(s, source_ref: str) -> KBIngestState:
    """Get-or-create the incremental cursor row for ``source_ref``."""
    state = (
        s.query(KBIngestState)
        .filter(KBIngestState.source_ref == source_ref)
        .first()
    )
    if state is None:
        state = KBIngestState(source_ref=source_ref[:191], status="idle")
        s.add(state)
        s.flush()
    return state


def _touch_state(s, state: KBIngestState, last_message_id=None, status="ok", detail=None):
    """Stamp the cursor row: advance ``last_message_id`` (when provided), set
    ``status``/``detail`` and ``last_synced_at = now``."""
    if last_message_id is not None:
        state.last_message_id = str(last_message_id)[:32]
    state.status = status
    state.detail = detail
    state.last_synced_at = datetime.now()


# ── docs ────────────────────────────────────────────────────────────────────


def _first_heading(text: str, fallback: str) -> str:
    """First markdown heading (``#`` stripped) or ``fallback`` if there is none."""
    for line in text.split("\n"):
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title
    return fallback


def _doc_paths() -> list[str]:
    """Absolute paths of the doc files to mine (top-level set + docs/*.md, with
    docs/archive/ excluded by only globbing the top level)."""
    import glob

    paths: list[str] = []
    for name in _TOP_DOC_FILES:
        p = os.path.join(_REPO_ROOT, name)
        if os.path.isfile(p):
            paths.append(p)
    for p in sorted(glob.glob(os.path.join(_REPO_ROOT, "docs", "*.md"))):
        if os.path.isfile(p):
            paths.append(p)
    return paths


def mine_docs() -> dict:
    """Ingest repo markdown docs. Skips files whose content hash is unchanged.

    Returns ``{'documents': n_changed, 'chunks': n, 'skipped': n_unchanged}``.
    """
    documents = chunks = skipped = 0
    for path in _doc_paths():
        rel = os.path.relpath(path, _REPO_ROOT)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as e:  # noqa: BLE001
            print(f"[kb.miner] docs: could not read {rel}: {e}")
            continue
        data = content.encode("utf-8", errors="replace")
        sha = hashlib.sha256(data).hexdigest()
        meta = {"sha256": sha, "bytes": len(data)}
        with Session() as s:
            existing = (
                s.query(KBDocument)
                .filter(KBDocument.source_type == "doc", KBDocument.source_ref == rel[:191])
                .first()
            )
            if existing is not None and existing.meta_json:
                try:
                    if json.loads(existing.meta_json).get("sha256") == sha:
                        skipped += 1
                        continue
                except (ValueError, TypeError):
                    pass
            title = _first_heading(content, os.path.basename(path))
            doc = _upsert_document(s, "doc", rel, title=title, meta=meta)
            n = _replace_chunks(s, doc, chunk_markdown(content))
            s.commit()
            documents += 1
            chunks += n
    print(f"[kb.miner] docs: {documents} changed, {chunks} chunks, {skipped} skipped")
    return {"documents": documents, "chunks": chunks, "skipped": skipped}


# ── tickets ─────────────────────────────────────────────────────────────────


def _ticket_author_label(name: str, is_staff: bool, is_bot: bool, kind: str) -> str:
    """Render a transcript author label; system rows are attributed to 'system'."""
    if kind == "system":
        return "system"
    label = name or "unknown"
    if is_staff:
        label += " [staff]"
    if is_bot:
        label += " [bot]"
    return label


def mine_tickets() -> dict:
    """Ingest mirrored ticket transcripts (one doc per ticket that has messages).

    Change detection: a ticket is skipped when the max mirrored ``ticket_messages``
    row id recorded in the doc's meta is unchanged.

    Returns ``{'documents': n, 'chunks': n, 'skipped': n}``.
    """
    documents = chunks = skipped = 0
    with Session() as s:
        ticket_ids = [
            r[0]
            for r in s.query(TicketMessage.ticket_id)
            .distinct()
            .order_by(TicketMessage.ticket_id)
            .all()
        ]

    for tid in ticket_ids:
        with Session() as s:
            agg = (
                s.query(func.max(TicketMessage.id), func.count(TicketMessage.id))
                .filter(TicketMessage.ticket_id == tid)
                .first()
            )
            max_row_id = agg[0] if agg else None
            msg_count = int(agg[1]) if agg else 0
            if max_row_id is None:
                continue  # messages vanished between the two queries

            existing = (
                s.query(KBDocument)
                .filter(KBDocument.source_type == "ticket", KBDocument.source_ref == str(tid))
                .first()
            )
            if existing is not None and existing.meta_json:
                try:
                    if json.loads(existing.meta_json).get("max_row_id") == max_row_id:
                        skipped += 1
                        continue
                except (ValueError, TypeError):
                    pass

            ticket = s.query(Ticket).filter(Ticket.ticket_id == tid).first()
            rows = (
                s.query(TicketMessage)
                .filter(TicketMessage.ticket_id == tid)
                .order_by(TicketMessage.date_sent.asc(), TicketMessage.id.asc())
                .all()
            )

            message_dicts: list[dict] = []
            for m in rows:
                content = m.content or ""
                has_att = bool((m.attachments_json or "").strip())
                if not content.strip() and not has_att:
                    continue
                if has_att:
                    try:
                        n_att = len(json.loads(m.attachments_json))
                        content = (content + f" [attachments: {n_att}]").strip()
                    except (ValueError, TypeError):
                        pass
                message_dicts.append(
                    {
                        "author": _ticket_author_label(
                            m.author_name, m.is_staff, m.is_bot, m.kind
                        ),
                        "ts": m.date_sent,
                        "content": content,
                    }
                )

            if not message_dicts:
                skipped += 1
                continue

            ticket_type = ticket.type if ticket else ""
            subject = (ticket.subject if ticket else None) or "no subject"
            title = f"Ticket #{tid} ({ticket_type}): {subject}"[:255]
            meta = {
                "status": (ticket.status if ticket else None),
                "type": ticket_type,
                "message_count": msg_count,
                "max_row_id": max_row_id,
            }
            doc = _upsert_document(
                s,
                "ticket",
                str(tid),
                title=title,
                started_at=(ticket.date_added if ticket else None),
                author_label=(rows[0].author_name if rows else None),
                meta=meta,
            )
            n = _replace_chunks(s, doc, chunk_messages(message_dicts))
            s.commit()
            documents += 1
            chunks += n

    print(f"[kb.miner] tickets: {documents} docs, {chunks} chunks, {skipped} skipped")
    return {"documents": documents, "chunks": chunks, "skipped": skipped}


# ── forums (Discord REST) ────────────────────────────────────────────────────


def _forum_targets() -> list[tuple[str, str]]:
    """``(channel_id, kind)`` pairs from env; unset/empty entries skipped."""
    targets: list[tuple[str, str]] = []
    for env_key, kind in (
        ("SUGGESTIONS_FORUM_CHANNEL_ID", "suggestion"),
        ("BUGS_FORUM_CHANNEL_ID", "bug"),
    ):
        cid = (os.getenv(env_key) or "").strip()
        if cid:
            targets.append((cid, kind))
    return targets


def _discord_author(author) -> str:
    return str(
        getattr(author, "display_name", None)
        or getattr(author, "username", None)
        or getattr(author, "id", "unknown")
    )


async def _mine_forum_post(post, kind: str, result: dict) -> None:
    """Ingest one forum thread (post). All DB work runs via ``to_thread``."""
    # Collect messages first (starter + replies) so change detection can compare
    # the newest id before any (re)chunking work.
    starter = None
    try:
        starter = await post.fetch_message(post.id)
    except Exception:  # noqa: BLE001
        starter = None  # starter deleted (404) -> skip it, thread may still hold replies

    replies: list = []
    try:
        async for m in post.history(limit=0):
            replies.append(m)
    except Exception as e:  # noqa: BLE001
        print(f"[kb.miner] forum post {post.id}: history fetch failed: {e}")
    replies.reverse()  # history yields newest-first -> make chronological

    messages: list = []
    if starter is not None:
        messages.append(starter)
    for m in replies:
        if str(m.id) == str(post.id):
            continue  # the starter is already included (its id == the thread id)
        messages.append(m)
    if not messages:
        return

    newest_id = max(int(m.id) for m in messages)
    source_ref = str(post.id)

    def _unchanged() -> bool:
        with Session() as s:
            existing = (
                s.query(KBDocument)
                .filter(KBDocument.source_type == "forum", KBDocument.source_ref == source_ref)
                .first()
            )
            if existing is not None and existing.meta_json:
                try:
                    return json.loads(existing.meta_json).get("last_message_id") == str(newest_id)
                except (ValueError, TypeError):
                    return False
            return False

    if await asyncio.to_thread(_unchanged):
        result["skipped"] += 1
        return

    message_dicts: list[dict] = []
    for m in messages:
        content = m.content or ""
        attachments = getattr(m, "attachments", None)
        if attachments:
            content = (content + f" [attachments: {len(attachments)}]").strip()
        if not content.strip():
            continue
        message_dicts.append(
            {
                "author": _discord_author(m.author),
                "ts": _plain_datetime(m.created_at),
                "content": content,
            }
        )
    if not message_dicts:
        result["skipped"] += 1
        return

    title = f"[{kind}] {getattr(post, 'name', '') or 'thread'}"[:255]
    url = f"https://discord.com/channels/{_GUILD_ID}/{post.id}"
    meta = {"kind": kind, "message_count": len(messages), "last_message_id": str(newest_id)}
    texts = chunk_messages(message_dicts)

    def _write() -> int:
        with Session() as s:
            doc = _upsert_document(s, "forum", source_ref, title=title, url=url, meta=meta)
            n = _replace_chunks(s, doc, texts)
            s.commit()
            return n

    n = await asyncio.to_thread(_write)
    result["threads"] += 1
    result["chunks"] += n


async def mine_forums(bot) -> dict:
    """Ingest suggestion/bug forum threads over Discord REST.

    ``bot`` must be a logged-in ``interactions`` client (REST only is fine).
    Returns ``{'threads': n, 'chunks': n, 'skipped': n, 'errors': n}``.
    """
    result = {"threads": 0, "chunks": 0, "skipped": 0, "errors": 0}
    targets = _forum_targets()
    if not targets:
        print("[kb.miner] forums: no forum channels configured; nothing to do")
        return result

    for cid, kind in targets:
        try:
            forum = await bot.fetch_channel(int(cid))
        except Exception as e:  # noqa: BLE001
            print(f"[kb.miner] forums: channel {cid} unavailable: {e}")
            result["errors"] += 1
            continue

        posts: list = []
        try:
            posts = list(await forum.fetch_posts())
        except Exception as e:  # noqa: BLE001
            print(f"[kb.miner] forums: fetch_posts failed for {cid}: {e}")
        try:
            async for p in forum.archived_posts(limit=0):
                posts.append(p)
        except Exception as e:  # noqa: BLE001
            print(f"[kb.miner] forums: archived_posts failed for {cid}: {e}")

        print(f"[kb.miner] forums: channel {cid} ({kind}) has {len(posts)} posts")
        for i, post in enumerate(posts):
            try:
                await _mine_forum_post(post, kind, result)
            except Exception as e:  # noqa: BLE001
                print(f"[kb.miner] forums: post {getattr(post, 'id', '?')} failed: {e}")
                result["errors"] += 1
            if (i + 1) % 25 == 0:
                print(f"[kb.miner] forums: channel {cid} processed {i + 1}/{len(posts)}")
            await asyncio.sleep(0.5)  # be gentle with the Discord API

    print(f"[kb.miner] forums: {result}")
    return result


# ── chat (Discord REST) ──────────────────────────────────────────────────────


async def _mine_chat_channel(bot, cid: int, result: dict) -> None:
    """Ingest one chat channel incrementally (per-channel snowflake cursor).

    New messages since the stored cursor are grouped by calendar month and
    *appended* to the corresponding channel-month doc. All DB work via
    ``to_thread``."""
    state_ref = f"chat:{cid}"

    def _load_cursor() -> str | None:
        with Session() as s:
            state = _get_state(s, state_ref)
            s.commit()
            return state.last_message_id

    last_id = await asyncio.to_thread(_load_cursor)

    channel = await bot.fetch_channel(cid)
    channel_name = getattr(channel, "name", str(cid))
    if last_id:
        hist = channel.history(limit=0, after=int(last_id))
    else:
        hist = channel.history(limit=0)

    collected: list = []
    async for m in hist:
        collected.append(m)
        if len(collected) % 1000 == 0:
            print(f"[kb.miner] chat: channel {cid} collected {len(collected)}")
    if not collected:
        await asyncio.to_thread(
            lambda: _touch_and_commit(state_ref, None, "ok", "no new messages")
        )
        return

    collected.sort(key=lambda m: int(m.id))
    max_id = max(int(m.id) for m in collected)

    # Filter (drop bots and empty/attachment-only messages) and group by month.
    by_month: dict[int, list[dict]] = {}
    kept = 0
    for m in collected:
        if getattr(m.author, "bot", False):
            continue
        base = m.content or ""
        if not base.strip():
            continue  # empty content -> attachment-only messages are dropped
        attachments = getattr(m, "attachments", None)
        content = (base + f" [attachments: {len(attachments)}]") if attachments else base
        ts = _plain_datetime(m.created_at)
        if not isinstance(ts, datetime):
            continue
        yyyymm = ts.year * 100 + ts.month
        by_month.setdefault(yyyymm, []).append(
            {"author": _discord_author(m.author), "ts": ts, "content": content}
        )
        kept += 1

    def _write() -> int:
        appended = 0
        with Session() as s:
            for yyyymm in sorted(by_month):
                month_msgs = by_month[yyyymm]
                first_ts = month_msgs[0]["ts"]
                title = f"#{channel_name} — {first_ts:%Y-%m}"
                doc = _upsert_document(s, "chat", f"{cid}:{yyyymm}", title=title)
                appended += _append_chunks(s, doc, chunk_messages(month_msgs))
            state = _get_state(s, state_ref)
            _touch_state(
                s, state, last_message_id=str(max_id), status="ok",
                detail=f"{len(by_month)} months, {kept} msgs",
            )
            s.commit()
            return appended

    appended = await asyncio.to_thread(_write)
    result["messages"] += kept
    result["chunks"] += appended


def _touch_and_commit(state_ref: str, last_message_id, status: str, detail: str) -> None:
    """Stamp a cursor row in its own short session (used when there is nothing
    else to write)."""
    with Session() as s:
        state = _get_state(s, state_ref)
        _touch_state(s, state, last_message_id=last_message_id, status=status, detail=detail)
        s.commit()


async def mine_chat(bot, channel_ids: list[int]) -> dict:
    """Ingest general chat channels over Discord REST (incremental, append-only).

    Returns ``{'channels': n, 'messages': n, 'chunks': n}``.
    """
    result = {"channels": 0, "messages": 0, "chunks": 0}
    for cid in channel_ids:
        try:
            await _mine_chat_channel(bot, int(cid), result)
            result["channels"] += 1
        except Exception as e:  # noqa: BLE001
            print(f"[kb.miner] chat: channel {cid} failed: {e}")
            continue
    print(f"[kb.miner] chat: {result}")
    return result


# ── embedding pass ───────────────────────────────────────────────────────────


def embed_missing_chunks(batch_size: int = 64) -> int:
    """Fill in embeddings for chunks that have none, in sequential batches.

    No-op (returns 0) when the local embedding stack is unavailable. Commits per
    batch so the pass is resumable and memory stays flat. Returns the number of
    chunks embedded."""
    if not embedder.enabled():
        print("[kb.miner] embeddings disabled (embedder.enabled() is False); skipping")
        return 0

    with Session() as s:
        pending = (
            s.query(func.count(KBChunk.id)).filter(KBChunk.embedding.is_(None)).scalar()
        ) or 0
    if not pending:
        print("[kb.miner] embed: no chunks pending")
        return 0
    print(f"[kb.miner] embed: {pending} chunks pending")

    total = 0
    while True:
        with Session() as s:
            rows = (
                s.query(KBChunk.id, KBChunk.content)
                .filter(KBChunk.embedding.is_(None))
                .order_by(KBChunk.id.asc())
                .limit(batch_size)
                .all()
            )
            if not rows:
                break
            vectors = embedder.embed_passages([content for _, content in rows])
            for (chunk_id, _content), vector in zip(rows, vectors):
                s.query(KBChunk).filter(KBChunk.id == chunk_id).update(
                    {
                        KBChunk.embedding: vector,
                        KBChunk.embedding_model: embedder.MODEL_NAME,
                    },
                    synchronize_session=False,
                )
            s.commit()
            total += len(rows)
            print(f"[kb.miner] embedded {total}/{pending}")
    print(f"[kb.miner] embed: complete, {total} chunks")
    return total
