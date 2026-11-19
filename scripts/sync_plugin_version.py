#!/usr/bin/env python3
"""Advertise the plugin version the Plugin Hub has actually released.

    venv/bin/python -m scripts.sync_plugin_version            # report only
    venv/bin/python -m scripts.sync_plugin_version --apply

Reads the commit the Plugin Hub pins for DropTracker, the version that commit
builds, and how many players are already submitting with it. If the hub's
version is newer than the one we advertise and it is clearly in players'
hands, the advertised version (group 2, plugin_latest_version) is moved up
to it. Never down, never to anything the hub does not pin. See
utils/plugin_release.py. The same check runs every 15 minutes inside
droptracker-player-updates.
"""
import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the new version")
    args = parser.parse_args()

    from db.models.base import Session
    from utils import plugin_release

    session = Session()
    try:
        result = plugin_release.sync(session, apply=args.apply)
        if result["changed"]:
            session.commit()
    except Exception as exc:
        session.rollback()
        print(f"plugin version check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    state = "UPDATED" if result["changed"] else ("would update" if result.get("would_change") else "no change")
    print(f"{state}: {result['reason']} (hub commit {str(result['commit'])[:7]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
