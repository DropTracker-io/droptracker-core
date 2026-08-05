#!/usr/bin/env python3
"""Track the wom.py fork automatically: install new metrics and restart consumers.

The fork (``DropTracker-io/wom.py`` branch ``droptracker``) auto-syncs its
``Metric`` enum against ``@wise-old-man/utils`` — hourly on Wed/Thu (OSRS update
days), every 12h otherwise. That closes the gap between WOM publishing a new
boss and the *library* knowing about it, but historically a human still had to
notice the commit, bump the ``@sha`` pin in requirements.txt, reinstall, and
restart the services. Until they did, WOM-hybrid event tracking couldn't map a
``kc_target`` task for the new boss onto a WOM metric (it silently fell back to
plugin-only, logged by the reconciler).

This script closes that last gap. Run on a timer, it:

  1. compares the fork's branch HEAD against what is actually installed
     (``dist-info/direct_url.json`` — NOT ``pip show``, see below),
  2. installs the new commit,
  3. verifies it before trusting it (imports, enum didn't shrink, the
     WOM-dependent unit tests still pass) and ROLLS BACK if not,
  4. updates + commits the requirements.txt pin so CI and a fresh venv match
     prod (avoiding exactly the skew the fork was created to eliminate),
  5. restarts the services that hold the enum in memory,
  6. DMs a summary.

Nothing here is destructive on failure: a bad commit is rolled back to the sha
that was running and the services are left alone.

**The reinstall gotcha this exists to handle:** the fork's version string never
changes (always ``1.0.0+dt.1``), so ``pip install -r requirements.txt`` — even
with ``--upgrade`` — sees "same version already installed" and silently no-ops.
It clones the new sha, prepares metadata, then skips the install, and reports
success either way. Only ``--force-reinstall`` actually swaps the code, and only
``direct_url.json`` tells you which sha really landed.

Dry-run by default (repo convention); ``--apply`` to act.

Run:
    venv/bin/python -m scripts.sync_wom_fork              # report only
    venv/bin/python -m scripts.sync_wom_fork --apply      # install + restart
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

REPO_URL = "https://github.com/DropTracker-io/wom.py"
BRANCH = "droptracker"
DISC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(DISC, "venv", "bin", "python")
VENV_PIP = os.path.join(DISC, "venv", "bin", "pip")
REQUIREMENTS = os.path.join(DISC, "requirements.txt")
DIST_INFO_GLOB = os.path.join(DISC, "venv", "lib", "python*", "site-packages", "wom_py-*.dist-info", "direct_url.json")
NOTIFIER = "/home/debian/bin/notify_dm.py"

# Services that hold the wom enum in memory. A running process keeps the old
# module, so installing without restarting changes nothing for them.
SERVICES = [
    "droptracker-events",
    "droptracker-player-updates",
    "droptracker-webapi",
    "droptracker-core",
]

# Cheap, WOM-dependent unit tests. These are the ones that would break if the
# enum shape changed underneath us (they assert metric mapping), and they run in
# well under a second — worth it as a gate before restarting prod.
VERIFY_TESTS = [
    "tests/unit/test_event_wom_reconciler.py",
    "tests/unit/test_event_effort.py",
]

PIN_RE = re.compile(
    r"(wom\.py @ git\+https://github\.com/DropTracker-io/wom\.py@)([0-9a-fA-F]{40})"
)


def log(msg: str) -> None:
    print(f"[sync_wom_fork] {msg}", flush=True)


def run(cmd, **kw):
    """Run a command, capturing output. Never raises — callers inspect rc."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def remote_head() -> str | None:
    """Current tip of the fork's tracking branch."""
    r = run(["git", "ls-remote", REPO_URL, f"refs/heads/{BRANCH}"])
    if r.returncode != 0 or not r.stdout.strip():
        log(f"ERROR: git ls-remote failed: {r.stderr.strip()[:200]}")
        return None
    return r.stdout.split()[0]


def installed_sha() -> str | None:
    """The sha actually installed in the venv.

    Read from direct_url.json rather than ``pip show``: the fork's version
    string is identical across commits, so pip cannot distinguish them.
    """
    for path in glob.glob(DIST_INFO_GLOB):
        try:
            with open(path) as f:
                return json.load(f).get("vcs_info", {}).get("commit_id")
        except (OSError, ValueError):
            continue
    return None


def pinned_sha() -> str | None:
    try:
        with open(REQUIREMENTS) as f:
            match = PIN_RE.search(f.read())
    except OSError:
        return None
    return match.group(2) if match else None


def enum_fingerprint() -> dict | None:
    """Sizes of the metric families, used to catch a truncated/broken install.

    A grow or hold is fine; a shrink means the enum lost members, which would
    silently stop mapping metrics we previously recognised.
    """
    code = (
        "import json, wom;"
        "f=lambda n: len(getattr(wom, n, ()) or ());"
        "print(json.dumps({'metrics': len(list(wom.enums.Metric)),"
        " 'bosses': f('Bosses'), 'skills': f('Skills'), 'activities': f('Activities')}))"
    )
    r = run([VENV_PY, "-c", code], cwd=DISC)
    if r.returncode != 0:
        log(f"ERROR: enum fingerprint failed: {r.stderr.strip()[:300]}")
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def install(sha: str) -> bool:
    """Force-reinstall the fork at `sha`. See the module docstring for why
    --force-reinstall is mandatory rather than --upgrade."""
    r = run([
        VENV_PIP, "install", "--no-deps", "--force-reinstall",
        f"wom.py @ git+{REPO_URL}@{sha}",
    ], cwd=DISC)
    if r.returncode != 0:
        log(f"ERROR: pip install {sha[:7]} failed: {r.stderr.strip()[-500:]}")
        return False
    landed = installed_sha()
    if landed != sha:
        log(f"ERROR: pip reported success but installed sha is {landed} (wanted {sha})")
        return False
    return True


def verify(before: dict | None) -> tuple[bool, str]:
    """Confirm the new install is usable before anything is restarted."""
    after = enum_fingerprint()
    if after is None:
        return False, "the package does not import"
    if before:
        shrunk = [k for k, v in after.items() if v < before.get(k, 0)]
        if shrunk:
            return False, f"enum shrank ({', '.join(shrunk)}): {before} -> {after}"
    r = run([VENV_PY, "-m", "pytest", *VERIFY_TESTS, "-q"], cwd=DISC)
    if r.returncode != 0:
        return False, f"WOM unit tests failed:\n{r.stdout.strip()[-800:]}"
    return True, json.dumps(after)


def update_pin(sha: str) -> str:
    """Point requirements.txt at `sha` and commit/push it.

    Best-effort: the install is already live and verified by this point, so a
    git problem must not fail the run — it just means the pin is stale and a
    human should commit it. Returns a human-readable status.
    """
    try:
        with open(REQUIREMENTS) as f:
            content = f.read()
        new_content, count = PIN_RE.subn(rf"\g<1>{sha}", content)
        if not count:
            return "pin line not found in requirements.txt — commit it by hand"
        if new_content != content:
            with open(REQUIREMENTS, "w") as f:
                f.write(new_content)
    except OSError as e:
        return f"could not rewrite requirements.txt: {e}"

    git = ["git", "-C", DISC]
    # Only our own file — the tree is shared with in-flight work from other
    # sessions and must never be swept into this commit.
    if run(git + ["diff", "--quiet", "--", "requirements.txt"]).returncode == 0:
        return "pin already current"
    run(git + ["add", "requirements.txt"])
    msg = (
        f"deps: auto-bump wom.py fork pin to {sha[:7]}\n\n"
        "Installed and verified on prod by scripts/sync_wom_fork.py (timer), "
        "then services restarted. Committed so CI and a fresh venv match what "
        "is actually running."
    )
    if run(git + ["commit", "-m", msg]).returncode != 0:
        return "commit failed — pin updated on disk only"
    branch = run(git + ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    push = run(git + ["push", "origin", branch])
    if push.returncode != 0:
        return f"committed locally but push failed ({push.stderr.strip()[-200:]})"
    return f"committed + pushed to {branch}"


def restart_services() -> tuple[list[str], list[str]]:
    ok, failed = [], []
    for svc in SERVICES:
        cmd = ["systemctl", "restart", f"{svc}.service"]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        if run(cmd).returncode == 0:
            ok.append(svc)
        else:
            failed.append(svc)
    return ok, failed


def notify(title: str, *lines: str) -> None:
    """Best-effort DM. Never fails the run."""
    if os.getenv("WOM_SYNC_NOTIFY", "1") != "1" or not os.path.exists(NOTIFIER):
        return
    try:
        subprocess.run([NOTIFIER, "--title", title, *lines], timeout=30,
                       capture_output=True)
    except Exception as e:  # notification is a nicety, not the job
        log(f"notification failed: {e}")


def report_status(ok: bool, changes: list[str], error: str | None = None) -> None:
    """Best-effort report to the Discord automation channel. Never fails the
    run (the reporter itself swallows everything, but the import can fail on a
    broken venv — exactly when this script still needs its exit code)."""
    try:
        from services.automation_updates import report_run_sync

        report_run_sync("wom_sync", ok=ok, changes=changes, error=error)
    except Exception as e:
        log(f"automation status report failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually install, commit the pin and restart services")
    ap.add_argument("--force", action="store_true",
                    help="reinstall even when the installed sha already matches")
    args = ap.parse_args()

    remote = remote_head()
    if not remote:
        if args.apply:
            report_status(False, [], "git ls-remote against the wom.py fork failed")
        return 1
    installed = installed_sha()
    pinned = pinned_sha()
    log(f"remote={remote[:7]} installed={(installed or 'none')[:7]} pinned={(pinned or 'none')[:7]}")

    if installed == remote and not args.force:
        # Pin drift with a correct install is worth fixing but not worth a
        # restart: what is running is already right.
        if pinned != remote and args.apply:
            pin_status = update_pin(remote)
            log(f"install current; correcting stale pin: {pin_status}")
            report_status(True, [f"Corrected stale requirements pin: {pin_status}"])
        else:
            log("up to date, nothing to do")
            if args.apply:
                report_status(True, [])  # status-message refresh only
        return 0

    if not args.apply:
        log(f"DRY RUN: would install {remote[:7]}, verify, restart {', '.join(SERVICES)}")
        log("re-run with --apply to act")
        return 0

    before = enum_fingerprint()
    log(f"installing {remote[:7]} (was {(installed or 'none')[:7]})")
    if not install(remote):
        notify("wom.py auto-sync FAILED",
               f"BLOCKED: could not install fork commit {remote[:7]} — pip failed. "
               f"Still running {(installed or 'unknown')[:7]}; services untouched.")
        report_status(False, [],
                      f"Could not install fork commit {remote[:7]} — pip failed; "
                      f"still running {(installed or 'unknown')[:7]}, services untouched")
        return 1

    ok, detail = verify(before)
    if not ok:
        log(f"VERIFICATION FAILED: {detail}")
        rolled_back = bool(installed) and install(installed)
        log("rolled back" if rolled_back else "ROLLBACK ALSO FAILED — venv may be broken")
        notify("wom.py auto-sync FAILED",
               f"BLOCKED: fork commit {remote[:7]} failed verification: {detail[:600]}",
               ("Rolled back to " + installed[:7] + "; services were never restarted.")
               if rolled_back else
               "ROLLBACK ALSO FAILED — the venv may be broken, needs a human now.")
        report_status(False, [],
                      f"Fork commit {remote[:7]} failed verification: {detail[:200]} — "
                      + ("rolled back, services never restarted"
                         if rolled_back else "ROLLBACK ALSO FAILED, needs a human"))
        return 1

    log(f"verified: {detail}")
    pin_status = update_pin(remote)
    log(f"pin: {pin_status}")
    restarted, failed = restart_services()
    log(f"restarted: {', '.join(restarted) or 'none'}"
        + (f" | FAILED: {', '.join(failed)}" if failed else ""))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    notify(
        "wom.py fork auto-updated",
        f"Installed fork commit {remote[:7]} (was {(installed or 'none')[:7]}) at {stamp}. "
        f"Enum now {detail}.",
        f"Pin: {pin_status}. Restarted: {', '.join(restarted) or 'none'}."
        + (f" FAILED TO RESTART: {', '.join(failed)}." if failed else ""),
    )
    changes = [
        f"wom.py fork updated {(installed or 'none')[:7]} → {remote[:7]}",
        f"Enum: {detail}",
        f"Pin: {pin_status}",
        f"Restarted: {', '.join(restarted) or 'none'}",
    ]
    if failed:
        report_status(False, changes, f"Failed to restart: {', '.join(failed)}")
    else:
        report_status(True, changes)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
