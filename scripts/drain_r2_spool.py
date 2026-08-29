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
  * Bounded per pass (``--limit``) and rate-limited (``--rate``) so a large
    backlog drains steadily instead of stampeding the consumer. The acceptor
    rate-limits at 100/s per client IP and every replay shares one source IP.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

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


def replay_body(base_url: str, body: bytes, content_type: str, timeout: float = 30.0):
    """POST one captured body back to the intake. Returns (status, text)."""
    import requests

    resp = requests.post(
        f"{base_url}/webhook",
        data=body,
        headers={"Content-Type": content_type, "X-DT-Replay": "r2-spool"},
        timeout=timeout,
    )
    return resp.status_code, resp.text[:200]


def drain_r2(args) -> int:
    client = _r2_client()
    bucket = os.environ.get("R2_SPOOL_BUCKET", "droptracker-intake-spool")

    paginator = client.get_paginator("list_objects_v2")
    pending = []
    for page in paginator.paginate(Bucket=bucket, Prefix=args.prefix):
        for obj in page.get("Contents", []):
            pending.append(obj["Key"])
            if len(pending) >= args.limit:
                break
        if len(pending) >= args.limit:
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

    replayed = skipped = failed = 0
    interval = 1.0 / args.rate if args.rate > 0 else 0.0

    for key in pending:
        try:
            obj = client.get_object(Bucket=bucket, Key=key)
            body = obj["Body"].read()
            content_type = obj.get("ContentType") or "application/octet-stream"
            guid = (obj.get("Metadata") or {}).get("guid", "")
        except Exception as exc:
            print(f"  ! unreadable {key}: {exc}")
            failed += 1
            continue

        try:
            status, text = replay_body(args.intake, body, content_type)
        except Exception as exc:
            print(f"  ! replay failed {key}: {exc}")
            failed += 1
            # A failing intake means the rest of this pass will fail too.
            break

        if status == 200:
            client.delete_object(Bucket=bucket, Key=key)
            replayed += 1
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
            skipped += 1
        else:
            print(f"  ? deferred {status} {key} guid={guid} :: {text}")
            failed += 1
            break

        if interval:
            time.sleep(interval)

    print(f"\nreplayed={replayed} rejected={skipped} deferred={failed}")
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

    entries = rc.lrange("webhook:dead", 0, args.limit - 1)
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
    ap.add_argument("--limit", type=int, default=500,
                    help="max entries per pass (default: 500)")
    ap.add_argument("--rate", type=float, default=20.0,
                    help="replays per second against the intake (default: 20)")
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
