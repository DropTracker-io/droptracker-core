"""Discord reporting for the background automation jobs.

Two jobs make unattended changes: the GitHub Pages publisher
(``data/player_total_updater.py`` -> ``utils/github.py``) and the WOM fork
sync (``scripts/sync_wom_fork.py``). This module posts what they did to the
automation channel, and maintains a single status message — always the
bottom-most message in the channel — showing both jobs' last result and next
run time.

Refresh policy: when a run changed nothing, the status message is edited in
place. When a change (or failure) message was just posted above it, the old
status message is deleted and a fresh one posted below, so the status card
never gets buried.

Both producers run in different processes under different users, so shared
state lives in Redis and delivery is gateway-less REST
(:class:`utils.discord_rest.DiscordRest`). Everything here is best-effort: a
report must never fail the job that called it, and must never stall the
player-updates systemd watchdog (the whole run is bounded by
``_REPORT_TIMEOUT``). Heavy imports stay inside functions so the module loads
under the unit-test conftest that stubs ``db``/``services``.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

JOBS = {
    "github_pages": "GitHub Pages publisher",
    "wom_sync": "WOM fork sync",
}

STATUS_MESSAGE_KEY = "automation:updates:status_message_id"
JOB_STATE_KEY = "automation:updates:job:{job}"
LOCK_KEY = "automation:updates:lock"

WOM_SYNC_TIMER = "droptracker-wom-sync.timer"
GITHUB_GATE_KEY = "github_update_last_timestamp"
GITHUB_GATE_MINUTES = 30

COLOR_OK = 0x2ECC71
COLOR_FAIL = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6

_REPORT_TIMEOUT = 60
_DESCRIPTION_LIMIT = 3500

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dev_mode() -> bool:
    return os.getenv("STATE") == "dev" or os.getenv("STATUS") == "dev"


def _channel_id() -> str:
    return (os.getenv("DISCORD_AUTOMATION_CHANNEL_ID") or "").strip()


def _token() -> str:
    """BOT_TOKEN, loading the repo .env first if the caller never did (the
    wom-sync oneshot doesn't)."""
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        try:
            from dotenv import load_dotenv

            load_dotenv(os.path.join(REPO_ROOT, ".env"))
            token = os.getenv("BOT_TOKEN", "")
        except Exception:
            pass
    return token


def _redis():
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception:
        return None


def _record_last_run(job: str, ok: bool, changes: int, error: Optional[str]) -> None:
    conn = _redis()
    if conn is None:
        return
    try:
        state = {
            "ts": int(time.time()),
            "ok": bool(ok),
            "changes": int(changes),
            "error": (error or "")[:400] or None,
        }
        conn.set(JOB_STATE_KEY.format(job=job), json.dumps(state))
    except Exception as e:
        print(f"[automation_updates] failed to record state for {job}: {e}")


def _load_job_state(job: str) -> Optional[dict]:
    conn = _redis()
    if conn is None:
        return None
    try:
        raw = conn.get(JOB_STATE_KEY.format(job=job))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)
    except Exception:
        return None


def next_run_github() -> Optional[int]:
    """Epoch of the publisher's next eligible run: the 30-minute Redis gate
    plus its window. Approximate — the loop's 10-minute wake cadence can land
    later."""
    conn = _redis()
    if conn is None:
        return None
    try:
        raw = conn.get(GITHUB_GATE_KEY)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        # Stored via datetime.now().isoformat() — naive local time.
        last = datetime.fromisoformat(raw)
        return int((last + timedelta(minutes=GITHUB_GATE_MINUTES)).timestamp())
    except Exception:
        return None


def _parse_next_elapse(value: str) -> Optional[int]:
    """``systemctl show -p NextElapseUSecRealtime --value`` output to epoch.
    The value is a calendar string like ``Wed 2026-08-05 14:32:23 UTC``
    (``n/a`` when the timer is inactive)."""
    value = (value or "").strip()
    if not value or value == "n/a":
        return None
    parts = value.split()
    # Leading day-of-week is present but not needed; timezone token trails.
    try:
        if len(parts) == 4:
            _dow, date_str, time_str, tz = parts
        elif len(parts) == 3:
            date_str, time_str, tz = parts
        else:
            return None
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        if tz in ("UTC", "GMT"):
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        # Some other local timezone name: treat as local time.
        return int(dt.timestamp())
    except Exception:
        return None


def next_run_wom() -> Optional[int]:
    try:
        out = subprocess.run(
            ["systemctl", "show", WOM_SYNC_TIMER,
             "-p", "NextElapseUSecRealtime", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        return _parse_next_elapse(out.stdout)
    except Exception:
        return None


def build_change_embed(job: str, ok: bool, changes: list, error: Optional[str] = None,
                       now: Optional[int] = None) -> dict:
    label = JOBS.get(job, job)
    now = int(now if now is not None else time.time())
    if ok:
        title = f"{label} — changes applied"
        lines = [f"• {line}" for line in changes]
    else:
        title = f"{label} — FAILED"
        lines = [f"• {line}" for line in changes]
        lines.append(f"❌ {error or 'unknown error'}")
    description = "\n".join(lines)
    if len(description) > _DESCRIPTION_LIMIT:
        description = description[:_DESCRIPTION_LIMIT] + "…"
    return {
        "title": title,
        "description": description,
        "color": COLOR_OK if ok else COLOR_FAIL,
        "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }


def _job_field(job: str, state: Optional[dict], next_run: Optional[int]) -> dict:
    label = JOBS.get(job, job)
    if not state:
        last_line = "Last run: never (no recorded run yet)"
    else:
        ts = int(state.get("ts") or 0)
        when = f"<t:{ts}:R>" if ts else "unknown"
        if state.get("ok"):
            count = int(state.get("changes") or 0)
            result = f"✅ {count} change(s)" if count else "✅ no changes"
        else:
            err = (state.get("error") or "unknown error").splitlines()[0]
            result = f"❌ FAILED: {err}"
        last_line = f"Last run: {when} — {result}"
    next_line = f"Next run: ≈ <t:{next_run}:R>" if next_run else "Next run: unknown"
    return {"name": label, "value": f"{last_line}\n{next_line}", "inline": False}


def build_status_embed(states: dict, next_runs: dict, now: Optional[int] = None) -> dict:
    now = int(now if now is not None else time.time())
    fields = [
        _job_field(job, states.get(job), next_runs.get(job))
        for job in JOBS
    ]
    any_failed = any(s and not s.get("ok") for s in states.values())
    return {
        "title": "🤖 Automation status",
        "fields": fields,
        "color": COLOR_FAIL if any_failed else COLOR_NEUTRAL,
        "footer": {"text": "Updated after every job run"},
        "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }


def _is_not_found(exc: Exception) -> bool:
    """Duck-typed 404 check — DiscordRest raises the interactions library's
    ``NotFound``, but matching on shape keeps this module (and its tests) free
    of that import."""
    return type(exc).__name__ == "NotFound" or getattr(exc, "status", None) == 404


async def _refresh_status(rest, conn, channel_id: str, *, reposted_above: bool) -> None:
    """Bring the status message up to date. Edits in place unless a change
    message was just posted above it (or it doesn't exist), in which case the
    old one is deleted and a fresh one posted so status stays bottom-most."""
    states = {job: _load_job_state(job) for job in JOBS}
    next_runs = {"github_pages": next_run_github(), "wom_sync": next_run_wom()}
    embed = build_status_embed(states, next_runs)
    payload = {"embeds": [embed]}

    stored = None
    try:
        raw = conn.get(STATUS_MESSAGE_KEY)
        if raw:
            stored = raw.decode() if isinstance(raw, bytes) else str(raw)
    except Exception:
        stored = None

    if stored and not reposted_above:
        try:
            await rest.edit_message(channel_id, stored, payload)
            return
        except Exception as e:
            if not _is_not_found(e):
                raise
            stored = None  # deleted by a human — fall through to repost
    if stored:
        try:
            await rest.delete_message(channel_id, stored)
        except Exception as e:
            if not _is_not_found(e):
                raise
    message_id = await rest.post_message(channel_id, payload)
    if message_id:
        try:
            conn.set(STATUS_MESSAGE_KEY, str(message_id))
        except Exception:
            pass


async def _report_run(job: str, *, ok: bool, changes: list, error: Optional[str]) -> None:
    _record_last_run(job, ok, len(changes), error)

    if _dev_mode():
        return
    channel_id = _channel_id()
    if not channel_id:
        return
    token = _token()
    if not token:
        print("[automation_updates] no BOT_TOKEN; skipping Discord report")
        return

    conn = _redis()
    lock_token = None
    if conn is not None:
        lock_token = uuid.uuid4().hex
        acquired = False
        for _ in range(3):
            try:
                if conn.set(LOCK_KEY, lock_token, nx=True, ex=20):
                    acquired = True
                    break
            except Exception:
                break
            await asyncio.sleep(2)
        if not acquired:
            # Worst case without the lock is a cosmetic double-refresh.
            print("[automation_updates] proceeding without lock")
            lock_token = None

    try:
        from utils.discord_rest import DiscordRest

        async with DiscordRest(token, user_agent="DropTracker-automation/1.0") as rest:
            posted_change = False
            if changes or not ok:
                message_id = await rest.post_message(
                    channel_id,
                    {"embeds": [build_change_embed(job, ok, changes, error)]},
                )
                posted_change = message_id is not None
            if conn is None:
                # No Redis means no message-id tracking; the change message
                # above is the best we can do this run.
                return
            await _refresh_status(rest, conn, channel_id, reposted_above=posted_change)
    finally:
        if conn is not None and lock_token:
            try:
                raw = conn.get(LOCK_KEY)
                held = raw.decode() if isinstance(raw, bytes) else raw
                if held == lock_token:
                    conn.delete(LOCK_KEY)
            except Exception:
                pass


async def report_run(job: str, *, ok: bool, changes: Optional[list] = None,
                     error: Optional[str] = None) -> None:
    """Record and report one job run. Never raises."""
    try:
        await asyncio.wait_for(
            _report_run(job, ok=ok, changes=list(changes or []), error=error),
            timeout=_REPORT_TIMEOUT,
        )
    except Exception as e:
        print(f"[automation_updates] report failed for {job}: {e}")


def report_run_sync(job: str, *, ok: bool, changes: Optional[list] = None,
                    error: Optional[str] = None) -> None:
    """Blocking wrapper for oneshot scripts. Never raises."""
    try:
        asyncio.run(report_run(job, ok=ok, changes=changes, error=error))
    except Exception as e:
        print(f"[automation_updates] sync report failed for {job}: {e}")
