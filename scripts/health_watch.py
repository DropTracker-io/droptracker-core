"""Watch the fleet and DM the owner when something is actually down.

Written after the 2026-08-02 outage, in which ``droptracker-api`` was dead for
2h31m and nothing noticed. Two things made that possible, and this script
exists to close both:

1. **"failed" is not the same as "down."** When MariaDB was OOM-killed, every
   hypercorn worker died and hypercorn's master exited with status **0**.
   systemd recorded that as ``inactive (dead)``, not ``failed`` — so
   ``systemctl list-units --state=failed`` reported a completely clean fleet
   while the intake API was refusing every connection. Any check built on the
   failed list would have missed it, and one did. This polls ``is-active`` for
   each unit we expect to be running, by name.

2. **Nothing watched the HTTP surface.** nginx returned 423,002 × 502 over
   those two and a half hours; the first signal anyone got was players opening
   support tickets. So the units being "active" is not enough — the ports get
   probed too.

Deliberately dependency-light, and deliberately a systemd timer rather than a
task inside a bot: a monitor that lives in the thing it monitors cannot report
the thing it monitors dying. It needs no database (a DB outage is one of the
things it must survive to report) and keeps its state in a small JSON file, so
Redis being down is something it can alert about rather than something that
disables it.

Alerting rule: a check must fail ``FAIL_THRESHOLD`` consecutive times before it
pages, which rides out a restart or a deploy. One message on the way down, one
on the way back up, and nothing in between — an alert that repeats every two
minutes for two hours is an alert people learn to ignore.

Run manually:  cd /store/droptracker/disc && venv/bin/python -m scripts.health_watch
Dry run:       ... --dry-run    (prints what it would send, DMs nothing)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO_ROOT, ".env"))

STATE_PATH = os.path.join(REPO_ROOT, "logs", "health_watch_state.json")

# Consecutive failures before we page. 2 with a 2-minute timer ≈ 4 minutes of
# genuine downtime, which a normal restart or blue-green deploy never reaches.
FAIL_THRESHOLD = 2

# Units expected to be running at all times. Mirrors the always-on entries of
# web_api.routes.admin.SERVICE_REGISTRY, duplicated on purpose: this script must
# not import the web stack it is watching. Oneshot units (the node deploy
# trigger, timers, backups) are excluded — they are *supposed* to be inactive.
WATCHED_UNITS = [
    ("droptracker-api", "RuneLite intake API"),
    ("droptracker-webapi", "Website backend API"),
    ("droptracker-core", "Discord bot (core)"),
    ("droptracker-webhooks", "Webhook reader bot"),
    ("droptracker-webhook-consumer", "Intake queue consumer"),
    ("droptracker-events", "Events consumer"),
    ("droptracker-lootboards", "Lootboard generator"),
    ("droptracker-player-updates", "Player/WOM updater"),
    ("droptracker-hof", "Hall of Fame bot"),
    ("droptracker-heartbeat", "Heartbeat bot"),
    ("droptracker-video-worker", "Video worker"),
    ("droptracker-adminbot", "Admin/KB bot"),
]

# The ports that actually serve players. A unit can be "active" while its
# workers are wedged, which is the whole reason these exist.
HTTP_CHECKS = [
    ("intake-api", "RuneLite intake API", "http://127.0.0.1:31323/ping"),
    ("web-api", "Website backend API", "http://127.0.0.1:31325/api/v1/health"),
]

# The website runs blue/green; exactly one is behind nginx at a time, so this
# is healthy when EITHER answers.
WEB_PORTS = [("website", "Website front-end", ["http://127.0.0.1:31380/",
                                               "http://127.0.0.1:31381/"])]

HTTP_TIMEOUT = 8

# Free space on the filesystem everything shares. This is not housekeeping: at
# zero, Redis can no longer write its RDB snapshot and — with
# stop-writes-on-bgsave-error — starts rejecting *every write command*. That
# freezes the submission queue in both directions while systemd and the HTTP
# probes above still report a perfectly healthy fleet. 2026-08-18: 87 minutes,
# ~35k submissions, noticed only because a user asked about a missing drop.
# Deliberately an early warning, well above zero.
DISK_PATH = "/"
DISK_MIN_FREE_GB = 8

# Redis writability. A read probe cannot see the failure above — MISCONF
# rejects writes only, so GET/PING keep answering normally throughout.
REDIS_PROBE_KEY = "health:watch:write-probe"

# Intake liveness, per transport. services/status_metrics keeps
# `status:metrics:{source}:players` as a ZSET scored with the unix time of each
# player's last processed submission, written after every successful dispatch.
# If the newest entry goes stale, submissions have stopped being processed —
# whatever the units and ports claim. Baseline traffic is ~750/min, so a
# quarter hour of silence is never normal.
INTAKE_SOURCES = [("api", "API intake"), ("webhook", "Webhook-path intake")]
INTAKE_STALL_SECONDS = 900

# Submission backlogs that mean data is sitting somewhere unprocessed.
#
# `webhook:dead` holds entries whose processing raised a non-retryable error —
# BRPOPLPUSH'd entries are the only copy, so this list *is* the data. Retryable
# faults never reach it (_requeue_with_backoff handles those), so every entry is
# a real submission nobody has replayed. A trickle is not worth a page; a rate
# is. Drain with `python -m scripts.drain_r2_spool --source dead --apply`.
DEAD_LETTER_KEY = "webhook:dead"
DEAD_LETTER_MAX = 25

# utils/webhook_spool is the acceptor's on-disk fallback for when Redis refuses
# the enqueue. It should be empty at all times: anything in it means Redis was
# rejecting writes and the consumer has not drained it back yet. Unlike the
# dead list, one file here is already worth knowing about.
SPOOL_MAX_PENDING = 0


def _load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = f"{STATE_PATH}.tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print(f"WARNING: could not persist state: {e}", file=sys.stderr)


def unit_is_active(unit: str) -> bool:
    """`systemctl is-active`. NOT the failed list — see the module docstring."""
    try:
        out = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() == "active"
    except Exception:
        return False


def http_ok(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def disk_free_gb(path: str) -> float:
    import shutil

    return shutil.disk_usage(path).free / (1024 ** 3)


def _redis_client():
    """Redis handle, or None if the library/connection is unavailable.

    Shares DB_PASS as the password (see utils/redis.py). Kept local so a Redis
    problem is something this script reports rather than something that stops
    it running.
    """
    try:
        import redis

        return redis.Redis(
            host="127.0.0.1", port=6379, db=0, password=os.getenv("DB_PASS"),
            socket_connect_timeout=5, socket_timeout=5,
        )
    except Exception:
        return None


def redis_writable() -> tuple:
    """(ok, detail). Probes with a WRITE, because only writes reveal MISCONF."""
    client = _redis_client()
    if client is None:
        return False, "redis client unavailable"
    try:
        client.set(REDIS_PROBE_KEY, int(time.time()), ex=300)
        return True, ""
    except Exception as e:
        # The MISCONF text is long; keep enough to identify it in a DM.
        return False, str(e)[:220]


def intake_idle_seconds(source: str):
    """Seconds since this transport last processed anything, or None.

    None means "cannot tell" — no client, or the ZSET is empty. Empty is
    genuinely ambiguous (the set is trimmed on read), so it is treated as
    unknown and skipped rather than paged on; the disk and Redis probes are the
    primary detectors and this is the backstop that catches everything else.
    """
    client = _redis_client()
    if client is None:
        return None
    try:
        newest = client.zrevrange(f"status:metrics:{source}:players", 0, 0, withscores=True)
        if not newest:
            return None
        return max(0.0, time.time() - float(newest[0][1]))
    except Exception:
        return None


def dead_letter_depth():
    """Entries in webhook:dead, or None if it cannot be read."""
    client = _redis_client()
    if client is None:
        return None
    try:
        return int(client.llen(DEAD_LETTER_KEY))
    except Exception:
        return None


def spool_pending():
    """Files in the acceptor's disk spool, or None if it cannot be read."""
    try:
        from utils import webhook_spool
        return int(webhook_spool.pending_count())
    except Exception:
        return None


def run_checks() -> list:
    """Every check as (key, label, ok, detail), in one pass."""
    results = []
    for unit, label in WATCHED_UNITS:
        ok = unit_is_active(unit)
        results.append((f"unit:{unit}", f"{label} ({unit})", ok,
                        "unit is not active" if not ok else ""))
    for key, label, url in HTTP_CHECKS:
        ok = http_ok(url)
        results.append((f"http:{key}", f"{label} — {url}", ok,
                        "no healthy response" if not ok else ""))
    for key, label, urls in WEB_PORTS:
        ok = any(http_ok(u) for u in urls)
        results.append((f"http:{key}", f"{label} (blue/green)", ok,
                        "neither blue nor green answered" if not ok else ""))

    try:
        free_gb = disk_free_gb(DISK_PATH)
        results.append((f"disk:{DISK_PATH}", f"Disk space on {DISK_PATH}",
                        free_gb >= DISK_MIN_FREE_GB,
                        f"{free_gb:.1f} GiB free, below the {DISK_MIN_FREE_GB} GiB floor "
                        f"— Redis stops accepting writes when this reaches zero"))
    except Exception as e:
        results.append((f"disk:{DISK_PATH}", f"Disk space on {DISK_PATH}", False,
                        f"could not stat {DISK_PATH}: {e}"))

    ok, detail = redis_writable()
    results.append(("redis:writable", "Redis accepting writes", ok,
                    detail or "write probe rejected"))

    for source, label in INTAKE_SOURCES:
        idle = intake_idle_seconds(source)
        if idle is None:
            continue
        results.append((f"intake:{source}", label, idle <= INTAKE_STALL_SECONDS,
                        f"nothing processed for {idle / 60:.0f} min"))

    dead = dead_letter_depth()
    if dead is not None:
        results.append(("queue:dead", "Dead-lettered submissions",
                        dead <= DEAD_LETTER_MAX,
                        f"{dead} entries in {DEAD_LETTER_KEY} — these are the only "
                        f"copy. Replay: python -m scripts.drain_r2_spool "
                        f"--source dead --apply"))

    pending = spool_pending()
    if pending is not None:
        results.append(("queue:spool", "Acceptor disk spool",
                        pending <= SPOOL_MAX_PENDING,
                        f"{pending} entry(s) spooled to disk — Redis refused the "
                        f"enqueue and the consumer has not drained them back"))

    return results


async def send_dm(discord_id: str, content: str) -> bool:
    from utils.discord_rest import DiscordRest

    token = os.getenv("BOT_TOKEN", "")
    if not token:
        print("no BOT_TOKEN — cannot alert", file=sys.stderr)
        return False
    try:
        async with DiscordRest(token) as rest:
            await rest.send_dm(discord_id, {"content": content[:1900]})
        return True
    except Exception as e:
        print(f"alert DM failed: {e}", file=sys.stderr)
        return False


def owner_ids() -> list:
    raw = os.getenv("WEB_SUPERADMIN_DISCORD_IDS", "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Alert when a core service is down.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent; DM nothing")
    args = parser.parse_args()

    state = _load_state()
    results = run_checks()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    newly_down, recovered = [], []
    for key, label, ok, detail in results:
        entry = state.get(key) or {"fails": 0, "alerted": False}
        if ok:
            if entry.get("alerted"):
                recovered.append((label, entry.get("since")))
            state[key] = {"fails": 0, "alerted": False}
        else:
            fails = int(entry.get("fails") or 0) + 1
            alerted = bool(entry.get("alerted"))
            since = entry.get("since") or now
            if fails >= FAIL_THRESHOLD and not alerted:
                newly_down.append((label, detail))
                alerted = True
            state[key] = {"fails": fails, "alerted": alerted, "since": since}

    down_now = [r for r in results if not r[2]]
    print(f"[health_watch] {len(results) - len(down_now)}/{len(results)} healthy"
          + (f" — DOWN: {', '.join(r[1] for r in down_now)}" if down_now else ""))

    messages = []
    if newly_down:
        lines = "\n".join(f"• **{label}** — {detail}" for label, detail in newly_down)
        messages.append(
            f"🔴 **DropTracker service alert** ({now})\n{lines}\n\n"
            f"Failed {FAIL_THRESHOLD} checks in a row. Units auto-restart on exit; "
            f"still down means it is failing to start or was stopped by hand.\n"
            f"`systemctl status <unit>` · `journalctl -u <unit> -n 50`"
        )
    if recovered:
        lines = "\n".join(
            f"• **{label}**" + (f" (down since {since})" if since else "")
            for label, since in recovered
        )
        messages.append(f"🟢 **Recovered** ({now})\n{lines}")

    if messages and not args.dry_run:
        for discord_id in owner_ids():
            for msg in messages:
                asyncio.run(send_dm(discord_id, msg))
    elif messages:
        print("--- would send ---")
        for m in messages:
            print(m)

    _save_state(state)
    # Always exit 0: the alert IS the output. A non-zero exit would just add a
    # second, noisier failure notification from systemd for the same event.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
