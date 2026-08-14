"""Retention for direct-URL file transfers (web95a).

The ``/file-transfer`` page lets any signed-in user push an arbitrary 25 MB
file to staff, and staff answer with updated versions of it. Nothing about that
exchange is worth keeping forever, and nothing is capped per user — retention
is the only thing bounding what the bucket holds, so this pass is what makes
the feature safe to leave switched on.

Policy: a transfer dies 30 days after its **most recent** version, not after
its first. ``file_transfers.expires_at`` already carries that stamp (the API
recomputes it whenever a version is added, so a staff reply on day 29 buys the
whole transfer another 30 days). This script simply acts on rows whose stamp
has passed: delete every version's B2 object, then delete the rows.

Order matters. Objects go first and the DB rows second, because the row is the
only record of the object key — dropping it first would orphan the bytes in the
bucket with nothing left pointing at them. A crash between the two leaves the
row present with dead keys, which the next run cleans up idempotently (a
delete of a missing B2 key succeeds).

Safety:
  * Dry-run by default; ``--apply`` is required to delete anything.
  * Every deletion is recorded to ``logs/prune_file_transfers_<ts>.tsv``
    (transfer_id, version, storage_key, bytes) BEFORE it happens.
  * ``--grace-days`` adds slack past ``expires_at`` if you want a safety
    margin; ``--limit`` bounds a single run.
  * Only keys inside the ``dt_transfers/`` namespace are ever deleted, so a
    malformed row can never make this touch proof images or videos.

Run (dry-run):
    cd /store/droptracker/disc && venv/bin/python -m scripts.prune_file_transfers
Then, once the numbers look right:
    cd /store/droptracker/disc && venv/bin/python -m scripts.prune_file_transfers --apply

Scheduled daily via droptracker-prune-transfers.timer.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import FileTransfer, FileTransferVersion, Session  # noqa: E402

#: Guard rail: the transfers namespace, and nothing else, is deletable here.
KEY_PREFIX = "dt_transfers/"

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _fmt_bytes(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < step:
            return f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TiB"


def is_prunable_key(key: str | None) -> bool:
    """Whether a stored key is one this script is allowed to delete.

    Rows are written by our own upload path so this should always hold; it
    exists so that a hand-edited or corrupted row can't redirect the sweep at
    ``dt_uploads/`` (proof screenshots) or the video bucket paths.
    """
    return bool(key) and key.startswith(KEY_PREFIX) and ".." not in key


def cutoff(now: datetime, grace_days: int) -> datetime:
    """Rows with ``expires_at`` at or before this moment are due for pruning."""
    return now - timedelta(days=grace_days)


def _delete_keys(keys: list[str]) -> int:
    """Delete every key in one event loop; return how many failed.

    ``delete_object`` swallows its own exceptions and returns False, and a
    delete of an already-missing key succeeds — which is what lets a run that
    died halfway simply be repeated.
    """
    from utils.b2_storage import delete_object

    async def _run() -> list[bool]:
        return [await delete_object(key) for key in keys]

    return sum(1 for ok in asyncio.run(_run()) if not ok)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: dry run)")
    parser.add_argument("--grace-days", type=int, default=0,
                        help="extra days to wait past expires_at (default: 0)")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many transfers (0 = no limit)")
    args = parser.parse_args()

    due = cutoff(datetime.now(), args.grace_days)
    session = Session()
    try:
        query = (
            session.query(FileTransfer)
            .filter(FileTransfer.expires_at <= due)
            .order_by(FileTransfer.expires_at)
        )
        if args.limit:
            query = query.limit(args.limit)
        transfers = query.all()

        if not transfers:
            print(f"Nothing expired as of {due:%Y-%m-%d %H:%M} — nothing to do.")
            return 0

        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_path = os.path.join(LOG_DIR, f"prune_file_transfers_{stamp}.tsv")

        removed_objects = 0
        skipped_keys = 0
        held_back = 0
        dropped_rows = 0
        freed = 0

        with open(snap_path, "w", encoding="utf-8") as snap:
            snap.write("transfer_id\tversion\tstorage_key\tbytes\taction\n")
            for transfer in transfers:
                versions = (
                    session.query(FileTransferVersion)
                    .filter(FileTransferVersion.transfer_id == transfer.id)
                    .order_by(FileTransferVersion.version)
                    .all()
                )
                deletable = [v for v in versions if is_prunable_key(v.storage_key)]
                foreign = [v for v in versions if not is_prunable_key(v.storage_key)]

                for v in foreign:
                    skipped_keys += 1
                    snap.write(
                        f"{transfer.id}\t{v.version}\t{v.storage_key}\t"
                        f"{v.size_bytes or 0}\tskipped_foreign_key\n"
                    )
                for v in deletable:
                    snap.write(
                        f"{transfer.id}\t{v.version}\t{v.storage_key}\t"
                        f"{v.size_bytes or 0}\tdeleted\n"
                    )
                snap.flush()

                failures = 0
                if args.apply and deletable:
                    failures = _delete_keys([v.storage_key for v in deletable])
                removed_objects += len(deletable) - failures
                freed += sum(int(v.size_bytes or 0) for v in deletable)

                # The row is the only record of these keys, so it may only go
                # once every object it points at is gone. A foreign key we
                # refused to touch, or a delete that failed, holds the whole
                # transfer back for a human (and for the next run, which is
                # free to retry — deleting an already-missing key succeeds).
                if foreign or failures:
                    held_back += 1
                    continue
                if args.apply:
                    session.delete(transfer)  # cascade drops the version rows
                dropped_rows += 1
            if args.apply:
                session.commit()

        verb = "freed" if args.apply else "would free"
        print(f"expired transfers  : {len(transfers):,}")
        print(f"objects {'deleted' if args.apply else 'to delete'}   : {removed_objects:,}")
        print(f"rows {'dropped' if args.apply else 'to drop'}      : {dropped_rows:,}")
        print(f"space {verb}       : {_fmt_bytes(freed)}")
        if skipped_keys:
            print(f"foreign keys kept  : {skipped_keys:,} (outside {KEY_PREFIX})")
        if held_back:
            print(f"transfers held back: {held_back:,} (objects still present; retried next run)")
        print(f"log                : {snap_path}")
        if not args.apply:
            print("\nDry run — nothing was changed. Re-run with --apply to act.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
