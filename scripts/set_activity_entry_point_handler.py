"""
Flip a Discord app's Activity Entry Point command between handler modes.

  handler 2 = DISCORD_LAUNCH_ACTIVITY : Discord launches + posts a channel message
  handler 1 = APP_HANDLER            : our bot handles the launch (services/activity_launch.py)

Switching to 1 stops Discord's automatic per-launch channel message. Only do it
once the bot handler is deployed AND a bot is connected to that app's gateway,
or the Launch button will fail.

Usage (dry-run prints current state):
  venv/bin/python -m scripts.set_activity_entry_point_handler --token-env BOT_TOKEN
  venv/bin/python -m scripts.set_activity_entry_point_handler --token-env BOT_TOKEN --set 1
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

from dotenv import dotenv_values

UA = "DiscordBot (https://www.droptracker.io, 1.0)"
_ENTRY_POINT_TYPE = 4


def _req(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": UA,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-env", default="BOT_TOKEN", help="env var holding the bot token")
    ap.add_argument("--set", type=int, choices=(1, 2), help="new handler value; omit to just inspect")
    ap.add_argument("--env-file", default=".env")
    args = ap.parse_args()

    token = (dotenv_values(args.env_file).get(args.token_env) or "").strip().strip('"')
    if not token:
        print(f"{args.token_env} not set in {args.env_file}")
        return 1

    app = _req("GET", "/applications/@me", token)
    app_id = app["id"]
    embedded = bool(app.get("flags", 0) & (1 << 17))
    print(f"app {app_id} '{app.get('name')}' — Activities enabled: {embedded}")
    if not embedded:
        print("This app has no Activity (EMBEDDED flag) — nothing to do.")
        return 1

    cmds = _req("GET", f"/applications/{app_id}/commands", token)
    ep = next((c for c in cmds if c.get("type") == _ENTRY_POINT_TYPE), None)
    if not ep:
        print("No entry-point command found (is 'Enable Activities' set in the portal?).")
        return 1
    print(f"entry-point '{ep['name']}' id={ep['id']} current handler={ep.get('handler')}")

    if args.set is None:
        print("(dry run — pass --set 1 or --set 2 to change)")
        return 0
    if ep.get("handler") == args.set:
        print(f"handler already {args.set}; no change.")
        return 0

    updated = _req("PATCH", f"/applications/{app_id}/commands/{ep['id']}", token, {"handler": args.set})
    print(f"handler set to {updated.get('handler')} ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")
        sys.exit(1)
