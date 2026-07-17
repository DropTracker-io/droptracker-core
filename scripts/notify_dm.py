#!/usr/bin/env python3
"""Send a Discord DM from the DropTracker bot — for agent task-completion pings.

Zero dependencies (stdlib only), so it runs with system python without a venv.

Usage:
    scripts/notify_dm.py "Finished web48a: event leadership deployed."
    scripts/notify_dm.py --title "Session done" "Task 1 summary" "Task 2 summary"
    echo "summary text" | scripts/notify_dm.py

Each positional argument becomes its own bullet; with --title they're grouped
under a bold header. Falls back to reading stdin if no message args are given.
Exits 0 on success, non-zero (with an error on stderr) on failure.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_USER_ID = "528746710042804247"
API = "https://discord.com/api/v10"
MAX_LEN = 2000


def read_env_token(env_path: Path, key: str = "BOT_TOKEN") -> str:
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"error: {key} not found in {env_path}")


def api_post(path: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DropTracker-agent-notify/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"error: Discord API {e.code} on {path}: {body}")


def build_message(parts: list[str], title: str | None) -> str:
    if title:
        lines = [f"**{title}**"] + [f"- {p.strip()}" for p in parts if p.strip()]
        msg = "\n".join(lines)
    elif len(parts) > 1:
        msg = "\n".join(f"- {p.strip()}" for p in parts if p.strip())
    else:
        msg = parts[0].strip()
    if len(msg) > MAX_LEN:
        msg = msg[: MAX_LEN - 25] + "\n… (truncated)"
    return msg


def main() -> None:
    ap = argparse.ArgumentParser(description="DM a task summary via the DropTracker bot.")
    ap.add_argument("messages", nargs="*", help="summary text; multiple args become bullets")
    ap.add_argument("--title", help="bold header line above the summaries")
    ap.add_argument("--user", default=DEFAULT_USER_ID, help="recipient Discord user id")
    ap.add_argument("--env", type=Path, default=ENV_PATH, help="path to .env with BOT_TOKEN")
    args = ap.parse_args()

    parts = args.messages or ([sys.stdin.read()] if not sys.stdin.isatty() else [])
    parts = [p for p in parts if p.strip()]
    if not parts:
        ap.error("no message given (pass args or pipe via stdin)")

    token = read_env_token(args.env)
    channel = api_post("/users/@me/channels", token, {"recipient_id": args.user})
    api_post(f"/channels/{channel['id']}/messages", token, {
        "content": build_message(parts, args.title),
        # suppress link-embed previews so summaries stay compact
        "flags": 1 << 2,
    })
    print("sent")


if __name__ == "__main__":
    main()
