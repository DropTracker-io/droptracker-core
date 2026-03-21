#!/usr/bin/env python3
"""
WOM ID Audit Script
===================

This utility performs a slow, rate-limited reconciliation of the local
`players` table against WiseOldMan's API. It verifies the stored WOM IDs,
records any discrepancies, and captures transient API failures for later
review without mutating database state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

import httpx
from asynciolimiter import Limiter
from dotenv import load_dotenv

import wom
from wom import Err, Result

# Allow running this file directly (`python scripts/wom_id_audit.py`) by
# ensuring the repository root is importable for absolute imports like `db`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import Session
from db.models import Player
from utils.format import normalize_player_display_equivalence


DEFAULT_RATE = 100 / 65  # Matches core WiseOldMan client usage.
DEFAULT_SLEEP = 0.35
DEFAULT_BATCH_SIZE = 25
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.8
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_PROGRESS_EVERY = 100


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit local Player WOM IDs against WiseOldMan.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Number of players to fetch from the database per iteration.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of players to audit.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Initial offset into the players table.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP,
        help="Extra delay (seconds) between successful API requests.",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=DEFAULT_RATE,
        help="Limiter rate expressed as requests per second.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Maximum API retry attempts per player lookup.",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=DEFAULT_BACKOFF,
        help="Exponential backoff multiplier between retries.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/wom_id_audit.json"),
        help="Where to persist the audit report (JSON).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Append to an existing report file instead of overwriting.",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Explicit WiseOldMan API key (falls back to WOM_API_KEY env).",
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default="DropTracker-WOM-Audit",
        help="User agent string for WiseOldMan API calls.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Per-request timeout in seconds for WOM API calls.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=DEFAULT_PROGRESS_EVERY,
        help="Emit an INFO progress log every N processed players.",
    )
    return parser.parse_args(argv)


def ensure_output_path(path: Path) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_identity(payload: Any) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    if payload is None:
        return None, None, None
    wom_id = getattr(payload, "id", None)
    username = getattr(payload, "username", None)
    display_name = getattr(payload, "display_name", None)

    nested = getattr(payload, "player", None)
    if nested is not None:
        wom_id = getattr(nested, "id", wom_id)
        username = getattr(nested, "username", username)
        display_name = getattr(nested, "display_name", display_name)
    return wom_id, username, display_name


async def execute_with_retries(
    call: Callable[[], Awaitable[Result]],
    limiter: Limiter,
    retries: int,
    backoff: float,
    extra_sleep: float,
    request_timeout: float,
    operation_label: str,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    last_error: Optional[Dict[str, Any]] = None
    for attempt in range(1, retries + 1):
        logging.info("%s: attempt %s/%s", operation_label, attempt, retries)
        await limiter.wait()
        try:
            result = await asyncio.wait_for(call(), timeout=request_timeout)
            if getattr(result, "is_ok", False):
                if extra_sleep > 0:
                    await asyncio.sleep(extra_sleep)
                return result.unwrap(), None
            error_obj = result.unwrap_err()
            status_code = getattr(result, "status_code", None)
            if isinstance(error_obj, Err):
                last_error = {
                    "type": "api_error",
                    "detail": str(error_obj),
                    "status_code": status_code,
                }
            else:
                last_error = {
                    "type": "api_error",
                    "detail": repr(error_obj),
                    "status_code": status_code,
                }
        except httpx.HTTPError as exc:
            last_error = {"type": "http_error", "detail": str(exc)}
            logging.warning("%s: HTTP error on attempt %s/%s: %s", operation_label, attempt, retries, exc)
        except asyncio.TimeoutError:
            last_error = {
                "type": "timeout",
                "detail": f"Request timed out after {request_timeout:.2f}s",
            }
            logging.warning(
                "%s: timeout on attempt %s/%s after %.2fs",
                operation_label,
                attempt,
                retries,
                request_timeout,
            )
        except Exception as exc:  # pylint: disable=broad-except
            last_error = {"type": "exception", "detail": repr(exc)}
            logging.warning(
                "%s: unexpected error on attempt %s/%s: %r",
                operation_label,
                attempt,
                retries,
                exc,
            )

        if attempt < retries:
            exponent = backoff ** (attempt - 1)
            base_delay = max(extra_sleep, 0.1)
            jitter = random.uniform(0.0, base_delay)  # nosec B311
            delay = exponent * base_delay + jitter
            logging.info(
                "%s: retrying in %.2fs (attempt %s/%s)",
                operation_label,
                delay,
                attempt + 1,
                retries,
            )
            await asyncio.sleep(delay)
    return None, last_error


async def fetch_by_id(
    client: wom.Client,
    limiter: Limiter,
    wom_id: int,
    retries: int,
    backoff: float,
    extra_sleep: float,
    request_timeout: float,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    async def call() -> Result:
        return await client.players.get_details_by_id(player_id=wom_id)

    return await execute_with_retries(
        call,
        limiter,
        retries,
        backoff,
        extra_sleep,
        request_timeout,
        operation_label=f"id_lookup(wom_id={wom_id})",
    )


async def fetch_by_username(
    client: wom.Client,
    limiter: Limiter,
    username: str,
    retries: int,
    backoff: float,
    extra_sleep: float,
    request_timeout: float,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    async def call() -> Result:
        return await client.players.get_details(username=username)

    return await execute_with_retries(
        call,
        limiter,
        retries,
        backoff,
        extra_sleep,
        request_timeout,
        operation_label=f"username_lookup(username={username})",
    )


def load_resume_state(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_report(
    path: Path,
    metadata: Dict[str, Any],
    mismatches: List[Dict[str, Any]],
    missing: List[Dict[str, Any]],
    lookup_failures: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    stats: Dict[str, Any],
    processed_ids: set[int],
) -> None:
    payload = {
        "metadata": metadata,
        "stats": stats,
        "mismatches": mismatches,
        "missing": missing,
        "lookup_failures": lookup_failures,
        "errors": errors,
        "processed_player_ids": sorted(processed_ids),
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)


def fetch_player_batch(
    db_session: Session,
    offset: int,
    batch_size: int,
) -> List[Player]:
    query = (
        db_session.query(Player)
        .filter(Player.wom_id.isnot(None))
        .order_by(Player.player_id)
        .offset(offset)
        .limit(batch_size)
    )
    return list(query)


async def audit_players(args: argparse.Namespace) -> int:
    load_dotenv()

    api_key = args.api_key or os.getenv("WOM_API_KEY")
    if not api_key:
        logging.error("WOM_API_KEY is not configured. Aborting.")
        return 1

    limiter = Limiter(args.rate)
    client = wom.Client(api_key, user_agent=args.user_agent)

    db_session = Session()
    mismatches: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    lookup_failures: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    processed_ids: set[int] = set()

    ensure_output_path(args.output)

    metadata: Dict[str, Any] = {
        "generated_at": iso_now(),
        "output": str(args.output),
        "settings": {
            "batch_size": args.batch_size,
            "limit": args.limit,
            "offset": args.offset,
            "sleep": args.sleep,
            "rate": args.rate,
            "max_retries": args.max_retries,
            "retry_backoff": args.retry_backoff,
            "request_timeout": args.request_timeout,
            "progress_every": args.progress_every,
        },
        "progress": {
            "next_offset": args.offset,
            "last_written_at": None,
        },
    }

    stats = {
        "processed": 0,
        "matched": 0,
        "mismatches": 0,
        "missing": 0,
        "lookup_failures": 0,
        "errors": 0,
        "skipped_existing": 0,
    }

    offset = args.offset

    if args.resume and args.output.exists():
        logging.info("Resuming from existing report at %s", args.output)
        existing = load_resume_state(args.output)
        mismatches.extend(existing.get("mismatches", []))
        missing.extend(existing.get("missing", []))
        lookup_failures.extend(existing.get("lookup_failures", []))
        errors.extend(existing.get("errors", []))
        for key in stats:
            if key in existing.get("stats", {}) and isinstance(existing["stats"][key], int):
                stats[key] = existing["stats"][key]
        existing_processed_ids = existing.get("processed_player_ids", [])
        if isinstance(existing_processed_ids, list):
            for player_id in existing_processed_ids:
                if isinstance(player_id, int):
                    processed_ids.add(player_id)
        else:
            for entry in mismatches + missing + lookup_failures:
                player_id = entry.get("player_id")
                if isinstance(player_id, int):
                    processed_ids.add(player_id)
        existing_next_offset = (
            existing.get("metadata", {})
            .get("progress", {})
            .get("next_offset")
        )
        if isinstance(existing_next_offset, int):
            offset = max(args.offset, existing_next_offset)
        metadata["settings"]["offset"] = offset
        metadata["progress"]["next_offset"] = offset
        metadata["resumed_at"] = iso_now()

    logging.info(
        (
            "Starting WOM ID audit: output=%s offset=%s batch_size=%s limit=%s "
            "rate=%.3f sleep=%.3f timeout=%.1fs"
        ),
        args.output,
        offset,
        args.batch_size,
        args.limit,
        args.rate,
        args.sleep,
        args.request_timeout,
    )

    await client.start()
    logging.info("WOM client started successfully.")

    players_processed = 0

    try:
        while True:
            batch_start = time.monotonic()
            logging.info("Fetching player batch: offset=%s size=%s", offset, args.batch_size)
            batch = fetch_player_batch(db_session, offset, args.batch_size)
            logging.info(
                "Fetched player batch: offset=%s size=%s in %.2fs",
                offset,
                len(batch),
                time.monotonic() - batch_start,
            )

            if not batch:
                logging.info("No more players found from offset %s.", offset)
                break

            for player in batch:
                if args.limit is not None and players_processed >= args.limit:
                    logging.info("Limit reached (%s players).", args.limit)
                    break

                players_processed += 1
                stats["processed"] += 1

                if player.player_id in processed_ids:
                    stats["skipped_existing"] += 1
                    continue

                if stats["processed"] <= 5:
                    logging.info(
                        "Processing player #%s: player_id=%s name=%s wom_id=%s",
                        stats["processed"],
                        player.player_id,
                        player.player_name,
                        player.wom_id,
                    )

                record_time = iso_now()
                logging.debug(
                    "Auditing player_id=%s, player_name=%s, wom_id=%s",
                    player.player_id,
                    player.player_name,
                    player.wom_id,
                )

                details_by_id, error_by_id = await fetch_by_id(
                    client,
                    limiter,
                    int(player.wom_id),
                    args.max_retries,
                    args.retry_backoff,
                    args.sleep,
                    args.request_timeout,
                )

                if details_by_id:
                    remote_id, remote_username, remote_display = extract_identity(
                        details_by_id
                    )
                    normalized_match = normalize_player_display_equivalence(
                        player.player_name or ""
                    ) == normalize_player_display_equivalence(remote_display or remote_username or "")

                    if remote_id == player.wom_id and normalized_match:
                        stats["matched"] += 1
                    elif remote_id == player.wom_id:
                        stats["matched"] += 1
                        errors.append(
                            {
                                "player_id": player.player_id,
                                "player_name": player.player_name,
                                "wom_id": player.wom_id,
                                "kind": "name_mismatch",
                                "remote_username": remote_username,
                                "remote_display_name": remote_display,
                                "checked_at": record_time,
                            }
                        )
                        stats["errors"] += 1
                    else:
                        mismatch_entry = {
                            "player_id": player.player_id,
                            "player_name": player.player_name,
                            "wom_id_stored": player.wom_id,
                            "wom_id_remote": remote_id,
                            "remote_username": remote_username,
                            "remote_display_name": remote_display,
                            "checked_at": record_time,
                            "detected_via": "id_lookup",
                        }
                        mismatches.append(mismatch_entry)
                        processed_ids.add(player.player_id)
                        stats["mismatches"] += 1
                        continue

                    processed_ids.add(player.player_id)
                    continue

                # ID lookup failed – attempt username lookup for potential corrections.
                logging.debug(
                    "ID lookup failed for player_id=%s (%s), attempting username lookup.",
                    player.player_id,
                    player.player_name,
                )

                details_by_username, error_by_username = None, None
                if player.player_name:
                    details_by_username, error_by_username = await fetch_by_username(
                        client,
                        limiter,
                        player.player_name,
                        args.max_retries,
                        args.retry_backoff,
                        args.sleep,
                        args.request_timeout,
                    )

                if details_by_username:
                    remote_id, remote_username, remote_display = extract_identity(
                        details_by_username
                    )
                    mismatch_entry = {
                        "player_id": player.player_id,
                        "player_name": player.player_name,
                        "wom_id_stored": player.wom_id,
                        "wom_id_remote": remote_id,
                        "remote_username": remote_username,
                        "remote_display_name": remote_display,
                        "checked_at": record_time,
                        "detected_via": "username_lookup",
                    }
                    mismatches.append(mismatch_entry)
                    processed_ids.add(player.player_id)

                if args.progress_every > 0 and stats["processed"] % args.progress_every == 0:
                    logging.info(
                        (
                            "Progress: processed=%s matched=%s mismatches=%s "
                            "missing=%s lookup_failures=%s skipped=%s"
                        ),
                        stats["processed"],
                        stats["matched"],
                        stats["mismatches"],
                        stats["missing"],
                        stats["lookup_failures"],
                        stats["skipped_existing"],
                    )
                    stats["mismatches"] += 1
                else:
                    unresolved_entry = {
                        "player_id": player.player_id,
                        "player_name": player.player_name,
                        "wom_id_stored": player.wom_id,
                        "checked_at": record_time,
                    }
                    if error_by_id:
                        unresolved_entry["id_lookup_error"] = error_by_id
                    if error_by_username:
                        unresolved_entry["username_lookup_error"] = error_by_username

                    if error_by_id or error_by_username:
                        lookup_failures.append(unresolved_entry)
                        stats["lookup_failures"] += 1
                    else:
                        missing.append(unresolved_entry)
                        stats["missing"] += 1

                    processed_ids.add(player.player_id)

            offset += len(batch)
            metadata["progress"]["next_offset"] = offset
            metadata["progress"]["last_written_at"] = iso_now()
            write_report(
                args.output,
                metadata,
                mismatches,
                missing,
                lookup_failures,
                errors,
                stats,
                processed_ids,
            )

            if args.limit is not None and players_processed >= args.limit:
                break

        logging.info(
            (
                "Audit complete. Processed=%s matched=%s mismatches=%s "
                "missing=%s lookup_failures=%s errors=%s"
            ),
            stats["processed"],
            stats["matched"],
            stats["mismatches"],
            stats["missing"],
            stats["lookup_failures"],
            stats["errors"],
        )
        metadata["progress"]["last_written_at"] = iso_now()
        write_report(
            args.output,
            metadata,
            mismatches,
            missing,
            lookup_failures,
            errors,
            stats,
            processed_ids,
        )
    finally:
        close_coro = getattr(client, "aclose", None)
        stop_coro = getattr(client, "close", None)
        fallback_stop = getattr(client, "stop", None)
        try:
            if callable(close_coro):
                result = close_coro()
                if asyncio.iscoroutine(result):
                    await result
            elif callable(stop_coro):
                result = stop_coro()
                if asyncio.iscoroutine(result):
                    await result
            elif callable(fallback_stop):
                result = fallback_stop()
                if asyncio.iscoroutine(result):
                    await result
        finally:
            db_session.close()

    return 0


async def entrypoint(args: argparse.Namespace) -> int:
    try:
        return await audit_players(args)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        return 130


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging()
    return asyncio.run(entrypoint(args))


if __name__ == "__main__":
    sys.exit(main())

