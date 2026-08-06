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
