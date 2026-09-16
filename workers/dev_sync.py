"""Push the Bug Tester roster to the dev instance the moment it changes.

Runs in production as ``droptracker-dev-sync``. It blocks on a Redis list that
everything which changes the roster pushes to
(``services.tester_roster.notify_changed`` — ``/bug-tester``, the admin badge
page), so a new tester reaches dev in a second or two. It also re-reads the
roster every ``POLL_SECONDS`` on its own, which catches changes that arrive any
other way (an account claimed or renamed, a badge row edited by hand).

A snapshot is sent when its content differs from the last one dev accepted,
when a change was signalled, and at least every ``RESEND_SECONDS`` whatever
happened. That last rule is what repairs a dev database that was restored
from a dump since the previous push.

Configuration (production ``.env``)::

    DEV_SYNC_URL = https://dev-api.droptracker.io/dev-sync/testers
    DEV_SYNC_KEY = <a Fernet key; the same value goes in dev's .env>

With either unset the worker idles and says so once. On a dev instance it
refuses outright: dev only ever receives a roster.

Usage::

    python -m workers.dev_sync            # the service
    python -m workers.dev_sync --once     # push one snapshot now and exit
    python -m workers.dev_sync --dry-run  # show what would be sent, send nothing
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

log = logging.getLogger("dev_sync")

#: How long to wait for a change signal before re-reading the roster anyway.
POLL_SECONDS = 10
#: Send an unchanged snapshot at least this often.
RESEND_SECONDS = 600
#: Waits after consecutive failed pushes, in order; the last one repeats.
BACKOFF_SECONDS = (2, 5, 15, 30, 60)
HTTP_TIMEOUT_SECONDS = 10

_stop = threading.Event()


class Pusher:
    """Decides when to send, and sends. Everything outside is plumbing."""

    def __init__(self, url: str, key: str, post=None, clock=time.monotonic):
        self.url = url
        self.key = key
        self._post = post
        self._clock = clock
        self.last_fingerprint = None
        self.last_success = None
        self.failures = 0
        self.next_attempt = 0.0

    def due(self, fingerprint: str, requested: bool) -> bool:
        now = self._clock()
        if self.failures and now < self.next_attempt:
            return False
        return (
            requested
            or fingerprint != self.last_fingerprint
            or self.last_success is None
            or now - self.last_success >= RESEND_SECONDS
        )

    def _send(self, token: str):
        post = self._post
        if post is None:
            import requests

            post = requests.post
        return post(
            self.url,
            data=token.encode("ascii"),
            headers={"Content-Type": "text/plain", "User-Agent": "DropTracker-DevSync/1"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )

    def push(self, roster: dict, fingerprint: str) -> bool:
        from services import tester_roster

        try:
            response = self._send(tester_roster.seal(roster, self.key))
            status = response.status_code
            body = {}
            try:
                body = response.json() or {}
            except Exception:
                pass
        except Exception as exc:
            return self._failed(f"the request failed: {exc}")

        if status == 200 and body.get("status") in ("applied", "stale"):
            self.last_fingerprint = fingerprint
            self.last_success = self._clock()
            self.failures = 0
            if body.get("status") == "applied":
                changes = {k: body.get(k) for k in (
                    "users_added", "players_added", "awards_added", "awards_revoked")}
                if any(changes.values()) or body.get("skipped") or body.get("groups"):
                    log.info("dev applied the roster: testers=%s changes=%s groups=%s",
                             body.get("testers"), changes, body.get("groups"))
                for note in body.get("skipped") or ():
                    log.warning("dev skipped: %s", note)
            return True
        if status == 409:
            return self._failed("dev is applying another snapshot", quiet=True)
        return self._failed(f"dev answered {status}: {str(body or '')[:200]}")

    def _failed(self, reason: str, quiet: bool = False) -> bool:
        self.failures += 1
        wait = BACKOFF_SECONDS[min(self.failures, len(BACKOFF_SECONDS)) - 1]
        self.next_attempt = self._clock() + wait
        (log.info if quiet else log.warning)(
            "roster push failed (%s); retrying in %ss", reason, wait)
        return False


def load_current():
    from db.models import Session
    from services import tester_roster

    with Session() as session:
        roster = tester_roster.load_roster(session)
        session.rollback()  # end the read transaction; never idle in one
    return roster, tester_roster.fingerprint(roster)


def _redis_client():
    import redis

    return redis.Redis(host="127.0.0.1", port=6379, db=0, password=os.getenv("DB_PASS"),
                       socket_timeout=POLL_SECONDS + 10, socket_connect_timeout=5)


def wait_for_request(client) -> bool:
    """Block up to POLL_SECONDS for a change signal. True when one arrived."""
    from services.tester_roster import REQUEST_KEY

    if client is None:
        _stop.wait(POLL_SECONDS)
        return False
    try:
        item = client.blpop([REQUEST_KEY], timeout=POLL_SECONDS)
    except Exception as exc:
        log.warning("waiting for a roster change failed: %s", exc)
        _stop.wait(POLL_SECONDS)
        return False
    if item is None:
        return False
    try:
        client.delete(REQUEST_KEY)  # a burst of changes is one push
    except Exception:
        pass
    return True


def run(pusher: Pusher, client) -> None:
    log.info("pushing the Bug Tester roster to %s", pusher.url)
    requested = True
    while not _stop.is_set():
        try:
            roster, fingerprint = load_current()
            if pusher.due(fingerprint, requested):
                pusher.push(roster, fingerprint)
        except Exception as exc:
            log.warning("reading the roster failed: %s", exc)
        requested = wait_for_request(client)


def _stop_on_signal(signum, frame):
    _stop.set()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--once", action="store_true", help="push one snapshot and exit")
    parser.add_argument("--dry-run", action="store_true", help="show the snapshot, send nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    from services import tester_roster
    from utils.dev_guild_guard import is_dev_mode

    if args.dry_run:
        roster, fingerprint = load_current()
        print(f"fingerprint {fingerprint[:12]}: {len(roster['users'])} testers, "
              f"{len(roster['players'])} accounts, {len(roster['awards'])} active awards")
        for user in roster["users"]:
            names = [p["player_name"] for p in roster["players"] if p["user_id"] == user["user_id"]]
            print(f"  user {user['user_id']} ({user.get('username')}): {', '.join(names)}")
        return 0

    if is_dev_mode():
        log.error("this is a dev instance; it receives the roster and never pushes one")
        return 0 if args.once else _idle()

    url, key = tester_roster.sync_url(), tester_roster.sync_key()
    if not url or not key:
        log.warning("DEV_SYNC_URL and DEV_SYNC_KEY are not both set; nothing to do")
        return 1 if args.once else _idle()

    pusher = Pusher(url, key)
    if args.once:
        roster, fingerprint = load_current()
        return 0 if pusher.push(roster, fingerprint) else 1

    signal.signal(signal.SIGTERM, _stop_on_signal)
    signal.signal(signal.SIGINT, _stop_on_signal)
    try:
        client = _redis_client()
    except Exception as exc:
        log.warning("no Redis (%s); polling every %ss instead", exc, POLL_SECONDS)
        client = None
    run(pusher, client)
    return 0


def _idle() -> int:
    """Stay up without doing anything, so systemd does not restart-loop us."""
    signal.signal(signal.SIGTERM, _stop_on_signal)
    signal.signal(signal.SIGINT, _stop_on_signal)
    _stop.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
