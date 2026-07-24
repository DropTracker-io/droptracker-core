"""One-shot knowledgebase backfill / sync CLI.

Runs the KB ingesters (services/kb/miner.py) over the selected sources and then
(optionally) fills in missing chunk embeddings. Intended to be run as::

    python -m scripts.kb_mine --sources docs,tickets
    python -m scripts.kb_mine --sources forums,chat --chat-channels 123,456
    python -m scripts.kb_mine --sources docs --skip-embed

Sources ``docs`` and ``tickets`` are synchronous and DB-only. Sources
``forums`` and ``chat`` talk to Discord over REST *only*: the client is logged
in with ``bot.login()`` (HTTP auth) and never starts a gateway, so this is safe
to run while the live droptracker-hof service (which owns this token's single
gateway session) is running. See ``_run_discord`` for the teardown caveat.

Exit code is 0 on success, 1 if any requested source (or the embed pass) raised.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from services.kb.miner import (  # noqa: E402
    embed_missing_chunks,
    mine_chat,
    mine_docs,
    mine_forums,
    mine_tickets,
)

VALID_SOURCES = ("tickets", "docs", "forums", "chat")


def _run_sync(name: str, fn, results: dict, errors: list) -> None:
    """Run a synchronous source ingester, capturing its result or exception."""
    print(f"[kb_mine] === {name} ===")
    try:
        results[name] = fn()
    except Exception as e:  # noqa: BLE001
        errors.append((name, e))
        print(f"[kb_mine] {name} FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()


async def _run_discord(
    token: str,
    do_forums: bool,
    do_chat: bool,
    chat_ids: list[int],
    results: dict,
    errors: list,
) -> None:
    """Log a REST-only interactions client in and run the Discord ingesters."""
    import interactions

    bot = interactions.Client(token=token)
    # HTTP-only auth: login() validates the token and initializes bot.http for
    # REST calls (fetch_channel/fetch_message/history/fetch_posts). It does NOT
    # start a gateway/WebSocket, so it will not clash with the live
    # droptracker-hof gateway session using this same token.
    await bot.login(token)
    try:
        if do_forums:
            try:
                results["forums"] = await mine_forums(bot)
            except Exception as e:  # noqa: BLE001
                errors.append(("forums", e))
                print(f"[kb_mine] forums FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()
        if do_chat:
            try:
                results["chat"] = await mine_chat(bot, chat_ids)
            except Exception as e:  # noqa: BLE001
                errors.append(("chat", e))
                print(f"[kb_mine] chat FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        # Tear down via http.close(), NOT bot.stop(). Verified against the
        # installed interactions 5.14 source: Client.stop() (client.py ~1027)
        # calls http.close() and then ConnectionState.stop() (gateway/state.py
        # ~86), whose `if self.gateway is not None` guard is TRUE here because
        # `gateway` defaults to the MISSING sentinel (not None). MISSING.close
        # resolves to None via Missing.__getattr__, so `self.gateway.close()`
        # becomes `None()` and raises TypeError when no gateway was ever started.
        # http.close() closes the aiohttp REST session directly and is a safe
        # no-op if already closed (http_client.py ~569).
        try:
            await bot.http.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill/sync the DropTracker knowledgebase.")
    ap.add_argument(
        "--sources",
        default="tickets,docs",
        help="comma list from {tickets,docs,forums,chat} (default: tickets,docs)",
    )
    ap.add_argument(
        "--chat-channels",
        default=None,
        help="comma channel ids for chat mining (fallback: env KB_CHAT_CHANNEL_IDS)",
    )
    ap.add_argument("--skip-embed", action="store_true", help="skip the embedding pass")
    ap.add_argument(
        "--token-env",
        default="HALL_OF_FAME_BOT_TOKEN",
        help="env var holding the bot token for REST forum/chat mining",
    )
    args = ap.parse_args()

    load_dotenv()

    sources = [x.strip() for x in args.sources.split(",") if x.strip()]
    unknown = [x for x in sources if x not in VALID_SOURCES]
    if unknown:
        print(f"[kb_mine] unknown source(s): {unknown}; valid: {list(VALID_SOURCES)}")
        return 1

    chat_raw = args.chat_channels if args.chat_channels is not None else os.getenv("KB_CHAT_CHANNEL_IDS", "")
    chat_ids: list[int] = []
    for part in (chat_raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            chat_ids.append(int(part))
        except ValueError:
            print(f"[kb_mine] ignoring non-numeric chat channel id: {part!r}")

    results: dict = {}
    errors: list = []

    # Synchronous, DB-only sources first.
    if "docs" in sources:
        _run_sync("docs", mine_docs, results, errors)
    if "tickets" in sources:
        _run_sync("tickets", mine_tickets, results, errors)

    # Discord REST sources.
    do_forums = "forums" in sources
    do_chat = "chat" in sources and bool(chat_ids)
    if "chat" in sources and not chat_ids:
        print("[kb_mine] chat requested but no channel ids (--chat-channels / KB_CHAT_CHANNEL_IDS); skipping chat")
    if do_forums or do_chat:
        token = os.getenv(args.token_env)
        if not token:
            msg = f"{args.token_env} not set; cannot mine forums/chat"
            print(f"[kb_mine] {msg}")
            for src in ("forums", "chat"):
                if src in sources:
                    errors.append((src, RuntimeError(msg)))
        else:
            try:
                asyncio.run(_run_discord(token, do_forums, do_chat, chat_ids, results, errors))
            except Exception as e:  # noqa: BLE001
                errors.append(("discord", e))
                print(f"[kb_mine] discord run FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()

    # Embedding pass (resumable; no-op when embeddings are disabled).
    if not args.skip_embed:
        print("[kb_mine] === embed ===")
        try:
            results["embed"] = {"embedded": embed_missing_chunks()}
        except Exception as e:  # noqa: BLE001
            errors.append(("embed", e))
            print(f"[kb_mine] embed FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()

    print("\n[kb_mine] ===== summary =====")
    for name, res in results.items():
        print(f"  {name}: {res}")
    if errors:
        print(f"[kb_mine] {len(errors)} step(s) raised:")
        for name, e in errors:
            print(f"    {name}: {type(e).__name__}: {e}")
        return 1
    print("[kb_mine] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
