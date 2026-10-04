#!/usr/bin/env python3
"""
Report a tester client build to the automation channel
======================================================

The job that builds the Bug Tester client is private and lives outside this
repo. When it publishes a build, or fails to, it runs this to say so in
Discord and to update the "Tester client build" line of the automation
status card (services/automation_updates.py).

For a published build the message is one line naming the build, then the
titles of what changed in it. For a failure it is the error the job
recorded in its status file.

A report must never fail the build job. Whatever goes wrong here is printed
and the exit code is 0.

Usage (from the repo root):
    venv/bin/python -m scripts.tester_build_report published /path/to/status.json
    venv/bin/python -m scripts.tester_build_report failed /path/to/status.json
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

JOB = "tester_build"
MODES = ("published", "failed")

MAX_TITLES = 8
MAX_ERROR_CHARS = 300
_MAX_TITLE_CHARS = 150


def _load_env() -> None:
    """The repo .env, for the bot token and channel id the reporter reads.

    Done here rather than at import, so loading this module (the tests do)
    never pulls production settings into the process.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except Exception as exc:
        print(f"[tester_build_report] could not load .env: {exc}")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def published_changes(status, manifest) -> list:
    """The lines of a "build published" message: the build, then what changed."""
    from utils import tester_builds as tb

    if not manifest:
        # Published, but the manifest is not where the status file is. Say
        # what the status knows rather than nothing.
        named = tb.status_payload(status)["current_build_id"]
        return [f"Published {named or 'a new build'}"]
    build = tb.build_payload(manifest, include_since_release=False)
    head = f"Published {build['build_id']}"
    if build["reason"]:
        head += f" ({build['reason']})"
    titles = [change["title"] for change in build["changes"] if change["title"]]
    return [head] + [_clip(title, _MAX_TITLE_CHARS) for title in titles[:MAX_TITLES]]


def failure_error(status) -> str:
    """What the job said went wrong, short enough for the status card."""
    from utils import tester_builds as tb

    error = tb.status_payload(status)["error"] or "The build failed. No error was recorded."
    return error[:MAX_ERROR_CHARS]


def _run(argv) -> None:
    if len(argv) != 2 or argv[0] not in MODES:
        print("usage: python -m scripts.tester_build_report <published|failed> "
              "<path to status.json>")
        return
    mode, status_path = argv
    _load_env()

    from services.automation_updates import report_run_sync
    from utils import tester_builds as tb

    status = tb.read_json(status_path) or {}
    if mode == "published":
        # The job writes status.json beside current.json, so look there first.
        # It may run with a different TESTER_BUILDS_DIR than this repo's .env.
        beside = os.path.dirname(os.path.abspath(status_path))
        manifest = tb.load_current(beside) or tb.load_current()
        report_run_sync(JOB, ok=True, changes=published_changes(status, manifest))
    else:
        report_run_sync(JOB, ok=False, changes=[], error=failure_error(status))


def main(argv=None) -> int:
    try:
        _run(list(sys.argv[1:] if argv is None else argv))
    except Exception as exc:
        print(f"[tester_build_report] not reported: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
