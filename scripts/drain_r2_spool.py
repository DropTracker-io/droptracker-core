"""Replay submissions the edge Worker captured while the intake was unavailable.

Why this exists
---------------
``edge/intake-capture`` sits in front of ``POST /webhook`` at Cloudflare. When
the origin cannot take a submission -- a bad deploy, a crashed acceptor, a full
disk, the whole box gone -- the Worker writes the **raw multipart body** to R2
and only then tells the plugin we have it. This script is the other half: it
puts those bodies back through the normal intake.

It replays the raw body byte-for-byte rather than a parsed envelope, so the
submission takes exactly the path it would have taken originally, including the
image stash. During an outage the origin never wrote the screenshot, so the R2
object is the *only* copy of it.

Why replaying twice is safe
---------------------------
``data/submissions/common.ensure_can_create`` is unbounded in time and blind to
transport, so a submission that already landed is recognised and skipped no
matter how late the replay arrives. That property is load-bearing here and is
guarded by ``tests/unit/test_replay_window_fidelity.py::TestGuidDedupIsTransportBlind``
-- it was false for drops until 2026-08-18, and replaying the outage window
duplicated 35,619 rows before anyone noticed.

The Worker also spools a small random sample of *successful* requests
(``FORCE_SPOOL_SAMPLE``) so this path stays exercised between incidents. Those
replays are expected to be no-ops; that is the point.

Modes
-----
``--source r2``    (default) drain the R2 spool by re-POSTing to the intake.
``--source dead``  drain Redis ``webhook:dead`` by pushing entries back onto
                   ``webhook:queue``. These are envelopes, not raw bodies, so
                   they go back on the queue directly. Their ``image_tmp_path``
                   may no longer exist -- the consumer unlinks temp files after
                   processing -- in which case the submission is recovered
                   without its screenshot, which beats losing it.

Safety
------
  * Dry-run by default; ``--apply`` is required to change anything.
  * An object is deleted from R2 **only** after the intake answers 200. Any
    other response leaves it in place for the next pass.
  * Probes ``/ping`` first and refuses to run against a sick intake, so a pass
    during an ongoing outage does not burn the backlog against 503s.
  * Rate-limited (``--rate``, shared by ``--workers`` concurrent replays) and
    time-boxed (``--max-seconds``) rather than capped at a fixed count, so a
    multi-hour outage drains in one or two passes instead of trickling out at
    500 objects per 5 minutes. The acceptor rate-limits at 100/s per client IP
    and every replay shares one source IP, hence the 50/s default.
  * Backpressure: the pass pauses while ``webhook:queue`` is deeper than
    ``--max-queue-depth``, so a backlog cannot bury live submissions behind it.
    The acceptor answers in ~3ms; the consumer is the real bottleneck.
  * Original dating: each replay carries the Worker's ``captured_at`` as a
    signed ``X-DT-Received-At`` header (utils.replay_stamp), so recovered rows
    are dated when the edge first received them, not when the drain ran.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _load_env() -> None:
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _r2_client():
    import boto3
    from botocore.config import Config

    account = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        # Retries here would stack on top of our own per-object retry, and a
        # slow R2 must not hold the drain pass open indefinitely.
        config=Config(retries={"max_attempts": 2}, connect_timeout=10, read_timeout=30),
    )


def intake_is_healthy(base_url: str, timeout: float = 5.0) -> bool:
    import requests

    try:
        return requests.get(f"{base_url}/ping", timeout=timeout).status_code == 200
    except Exception:
        return False


def stamp_headers(captured_at: str) -> dict:
    """Signed original-receive-time headers, or {} when the stamp can't be signed
    (no key configured, or metadata missing/garbled): the row is then dated at
    accept time, as before."""
    from utils import replay_stamp

    sig = replay_stamp.sign(captured_at) if captured_at else None
    if not sig:
        return {}
    return {replay_stamp.STAMP_HEADER: replay_stamp.normalize(captured_at),
            replay_stamp.SIG_HEADER: sig}


def replay_body(base_url: str, body: bytes, content_type: str, timeout: float = 30.0,
                captured_at: str = ""):
    """POST one captured body back to the intake. Returns (status, text)."""
    import requests

    headers = {"Content-Type": content_type, "X-DT-Replay": "r2-spool"}
    headers.update(stamp_headers(captured_at))
    resp = requests.post(f"{base_url}/webhook", data=body, headers=headers, timeout=timeout)
    return resp.status_code, resp.text[:200]


class RateLimiter:
    """Spaces calls ``1/rate`` apart across every thread sharing it."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate and rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        if slot > now:
            time.sleep(slot - now)


def _queue_depth_reader():
    """``() -> int | None`` for webhook:queue, or None when Redis is unreachable
    (the drain then runs without backpressure rather than not at all)."""
    try:
        import redis

        rc = redis.Redis(host="127.0.0.1", port=6379, db=0,
                         password=os.environ.get("DB_PASS"),
                         socket_timeout=5, socket_connect_timeout=5)
        rc.ping()
    except Exception as exc:
        print(f"  (no backpressure: Redis unavailable: {exc})")
        return None

    def depth():
        try:
            return int(rc.llen("webhook:queue"))
        except Exception:
            return None

    return depth


def drain_r2(args) -> int:
    client = _r2_client()
    bucket = os.environ.get("R2_SPOOL_BUCKET", "droptracker-intake-spool")
    limit = args.limit if args.limit and args.limit > 0 else None

    paginator = client.get_paginator("list_objects_v2")
    pending = []
    for page in paginator.paginate(Bucket=bucket, Prefix=args.prefix):
        for obj in page.get("Contents", []):
            pending.append(obj["Key"])
            if limit and len(pending) >= limit:
                break
        if limit and len(pending) >= limit:
            break

    # Keys embed a zero-padded UTC date path and a millisecond timestamp, so
    # lexicographic order is chronological. Oldest first.
    pending.sort()

    if not pending:
        print("nothing to drain")
        return 0

    print(f"{len(pending)} object(s) to replay from r2://{bucket}/{args.prefix}")
    if not args.apply:
        for key in pending[:20]:
            print(f"  would replay {key}")
        if len(pending) > 20:
            print(f"  ... and {len(pending) - 20} more")
        print("\ndry run -- pass --apply to replay and delete")
        return 0

    counts = {"replayed": 0, "rejected": 0, "deferred": 0}
    counts_lock = threading.Lock()
    stop = threading.Event()
    limiter = RateLimiter(args.rate)
    deadline = (time.monotonic() + args.max_seconds) if args.max_seconds else None
    depth = _queue_depth_reader() if args.max_queue_depth else None

    def bump(name):
        with counts_lock:
            counts[name] += 1

    def wait_for_consumer() -> None:
        """Hold off while the consumer is behind; never past the deadline."""
        if depth is None:
            return
        announced = False
        while not stop.is_set():
            d = depth()
            if d is None or d <= args.max_queue_depth:
                return
            if deadline and time.monotonic() >= deadline:
                stop.set()
                return
            if not announced:
                print(f"  .. webhook:queue depth {d} > {args.max_queue_depth}, pausing")
                announced = True
            time.sleep(2)

    def replay_one(key: str) -> None:
        if stop.is_set():
            return
        try:
            obj = client.get_object(Bucket=bucket, Key=key)
            body = obj["Body"].read()
            content_type = obj.get("ContentType") or "application/octet-stream"
            meta = obj.get("Metadata") or {}
            guid = meta.get("guid", "")
        except Exception as exc:
            print(f"  ! unreadable {key}: {exc}")
            bump("deferred")
            return

        if stop.is_set():
            return
        limiter.wait()
        try:
            status, text = replay_body(args.intake, body, content_type,
                                       captured_at=meta.get("captured_at", ""))
        except Exception as exc:
            print(f"  ! replay failed {key}: {exc}")
            bump("deferred")
            # A failing intake means the rest of this pass will fail too.
            stop.set()
            return

        if status == 200:
            client.delete_object(Bucket=bucket, Key=key)
            bump("replayed")
        elif status in (400, 401, 403):
            # The intake will never accept this body. Leaving it would retry
            # forever, so move it aside for inspection instead of deleting.
            client.copy_object(
                Bucket=bucket,
                Key=f"rejected/{key}",
                CopySource={"Bucket": bucket, "Key": key},
            )
            client.delete_object(Bucket=bucket, Key=key)
            print(f"  - rejected {status} {key} guid={guid} :: {text}")
            bump("rejected")
        else:
            print(f"  ? deferred {status} {key} guid={guid} :: {text}")
            bump("deferred")
            stop.set()

    def replay_one_safely(key: str) -> None:
        try:
            replay_one(key)
        except Exception as exc:
            # e.g. R2 refused the delete after a 200: the object stays and the
            # next pass replays it again, which GUID dedup makes a no-op.
            print(f"  ! {key}: {exc}")
            bump("deferred")
            stop.set()

    workers = max(1, int(args.workers or 1))
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        in_flight = []
        for i, key in enumerate(pending):
            if stop.is_set():
                break
            if deadline and time.monotonic() >= deadline:
                print(f"  .. time budget ({args.max_seconds}s) reached; "
                      f"{len(pending) - i} object(s) left for the next pass")
                break
            if i % 50 == 0:
                wait_for_consumer()
                if stop.is_set():
                    break
            # Keep at most 2x workers queued so a stop or the deadline takes
            # effect promptly instead of after the whole list was submitted.
            in_flight = [f for f in in_flight if not f.done()]
            while len(in_flight) >= workers * 2:
                in_flight[0].result()
                in_flight = [f for f in in_flight if not f.done()]
            in_flight.append(pool.submit(replay_one_safely, key))

    elapsed = time.monotonic() - started
    print(f"\nreplayed={counts['replayed']} rejected={counts['rejected']} "
          f"deferred={counts['deferred']} in {elapsed:.0f}s")
    return 0


def drain_dead(args) -> int:
    import redis

    rc = redis.Redis(
        host="127.0.0.1",
        port=6379,
        db=0,
        password=os.environ.get("DB_PASS"),
        decode_responses=True,
    )

    entries = rc.lrange("webhook:dead", 0, (args.limit or 500) - 1)
    if not entries:
        print("webhook:dead is empty")
        return 0

    print(f"{len(entries)} entry(s) in webhook:dead")
    for raw in entries[:20]:
        try:
            payload = json.loads(raw).get("payload", {})
            embeds = payload.get("embeds") or [{}]
            fields = {f.get("name"): f.get("value") for f in (embeds[0].get("fields") or [])}
            print(f"  {fields.get('type','?'):<20} {fields.get('player_name','?'):<16} "
                  f"guid={fields.get('guid','?')}")
        except Exception:
            print(f"  <unparseable> {raw[:80]}")

    if not args.apply:
        print("\ndry run -- pass --apply to requeue")
        return 0

    requeued = 0
    for raw in entries:
        # Requeue first, remove second: a crash in between replays the entry,
        # which GUID dedup absorbs. The other order would lose it.
        # RPUSH is the consumer's pop end (the acceptor LPUSHes): dead entries
        # predate everything live, so they jump the line rather than queue
        # behind traffic that arrived hours after them.
        rc.rpush("webhook:queue", raw)
        rc.lrem("webhook:dead", 1, raw)
        requeued += 1

    print(f"\nrequeued={requeued} onto webhook:queue")
    return 0


def main() -> int:
    _load_env()

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--source", choices=("r2", "dead"), default="r2",
                    help="r2 spool (default) or the Redis dead-letter list")
    ap.add_argument("--limit", type=int, default=0,
                    help="max entries per pass (default: 0 = no cap; the pass is "
                         "time-boxed by --max-seconds instead). For --source dead "
                         "0 means 500.")
    ap.add_argument("--rate", type=float, default=50.0,
                    help="replays per second against the intake, shared by all "
                         "workers (default: 50; the acceptor allows 100/s per IP)")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent replays; R2 reads dominate per-object latency "
                         "(default: 8)")
    ap.add_argument("--max-seconds", type=int, default=1500,
                    help="stop starting new replays after this long; the rest wait "
                         "for the next pass (default: 1500, under the unit's 30m "
                         "TimeoutStartSec; 0 = no limit)")
    ap.add_argument("--max-queue-depth", type=int, default=1000,
                    help="pause while webhook:queue is deeper than this, so live "
                         "submissions are not buried (default: 1000; 0 = off)")
    ap.add_argument("--prefix", default="webhook/",
                    help="R2 key prefix to drain (default: webhook/)")
    ap.add_argument("--intake", default=os.environ.get("INTAKE_API_URL",
                                                       "http://127.0.0.1:31323"),
                    help="intake base URL")
    ap.add_argument("--skip-health-check", action="store_true",
                    help="replay even if /ping is not answering")
    args = ap.parse_args()

    if args.source == "dead":
        return drain_dead(args)

    if not args.skip_health_check and not intake_is_healthy(args.intake):
        print(f"intake at {args.intake} is not healthy -- refusing to drain.\n"
              f"Replaying into a sick intake burns the backlog against 503s.\n"
              f"Pass --skip-health-check to override.")
        return 1

    return drain_r2(args)


if __name__ == "__main__":
    raise SystemExit(main())
