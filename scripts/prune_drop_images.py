"""Retention for user-uploaded DROP screenshots (storage reclamation).

Every drop submission may carry a screenshot, and we kept all of them forever:
as of 2026-07-21 that is 837k files / 152 GiB under
``static/assets/img/user-upload/*/drop/`` — the bulk of a 95%-full disk, which
is what started failing the nightly DB backup (it aborts unless >= 25 GiB is
free). The overwhelming majority are junk: 153gp of Coal, a 250gp Uncut
sapphire — each with its own screenshot nobody will ever look at.

Policy (both conditions decide KEEP; anything else is pruned):

  * the drop's total value (``value * quantity``) is >= ``--min-value``
    (default 1,000,000), or
  * the drop is newer than ``--retention-days`` (default 30).

For a pruned drop we delete the file AND null the row's ``image_url``, so the
site/HOF/Discord embeds render "no screenshot" rather than a broken image.

Why the DB drives this and not the filenames: ``download_player_image`` writes
``{item}_{entry_id}.{ext}`` but drop processors pass ``entry_id=0``, so the
filename carries NO drop id (``Coal_0_19.jpg``). The row's ``image_url`` holds
the only reliable file->drop mapping, so we scan ``drops`` and derive paths
from it. Scanning is chunked by the indexed ``partition`` column (22 monthly
partitions over 172M rows) — a single unindexed full scan on ``image_url``
times out against MariaDB's read_timeout.

Safety:
  * Dry-run by default; ``--apply`` is required to delete anything.
  * Every removal is recorded to ``logs/prune_drop_images_<ts>.tsv``
    (drop_id, image_url, bytes) BEFORE it happens, so the DB side is
    reversible and you always know exactly what went.
  * A resolved path must sit under the user-upload root or it is refused —
    a malformed/hostile ``image_url`` can never escape the tree.
  * Idempotent: nulling ``image_url`` means a pruned row is never revisited.
  * Files already gone still get their ``image_url`` cleared (self-healing).

Run (dry-run):
    cd /store/droptracker/disc && venv/bin/python -m scripts.prune_drop_images
Then, once the numbers look right:
    cd /store/droptracker/disc && venv/bin/python -m scripts.prune_drop_images --apply

Scheduled daily via droptracker-prune-images.timer, where it only ever finds
the thin slice of drops that just aged past the retention window.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, text  # noqa: E402

from db.models.base import session  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The user-upload tree and the public URL it is served at (nginx: /img/...).
LOCAL_ROOT = "/store/droptracker/disc/static/assets/img/user-upload/"
URL_PREFIXES = (
    "https://www.droptracker.io/img/user-upload/",
    "https://droptracker.io/img/user-upload/",
    "http://www.droptracker.io/img/user-upload/",
    "http://droptracker.io/img/user-upload/",
    # Some historical rows stored the filesystem path directly.
    LOCAL_ROOT,
)

DEFAULT_MIN_VALUE = 1_000_000
DEFAULT_RETENTION_DAYS = 30
# Rows per partition query / ids per UPDATE. Keeps each statement well inside
# MariaDB's read_timeout and bounds memory on a 172M-row table.
SELECT_CHUNK = 20_000
UPDATE_CHUNK = 1_000


def local_path_for(image_url: str) -> str | None:
    """Absolute on-disk path for a stored ``image_url``, or None if it isn't a
    user-upload reference we own.

    Refuses anything that escapes the user-upload root once resolved, so a
    malformed row (``.../../../etc/passwd``) can never target another tree.
    """
    if not image_url:
        return None
    raw = image_url.strip()
    for prefix in URL_PREFIXES:
        if raw.startswith(prefix):
            relative = raw[len(prefix):]
            break
    else:
        return None
    relative = relative.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if not relative:
        return None
    candidate = os.path.realpath(os.path.join(LOCAL_ROOT, relative))
    root = os.path.realpath(LOCAL_ROOT)
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


def recap_protected_paths() -> set[str]:
    """On-disk screenshots a recap card points at, which must survive pruning.

    A recap is an archive: ``/groups/{id}/recap/{period}`` is meant to stay
    valid forever, and its biggest-drop card renders the screenshot straight
    from the frozen payload. ``services/recap.py`` captures the URL into the
    snapshot so the card can't be blanked by ``image_url`` being cleared — but
    that says nothing about the FILE, and a player's best month is very often
    worth under the prune threshold. Left alone, every low-value recap
    screenshot 404s 30 days after the month it immortalised.
    """
    rows = session.execute(text(
        "SELECT payload FROM recap_snapshots WHERE payload LIKE '%user-upload%'"
    )).all()

    urls: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "image_url" and isinstance(value, str):
                    urls.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for (payload,) in rows:
        try:
            walk(json.loads(payload))
        except (TypeError, ValueError):
            continue

    paths = {p for p in (local_path_for(u) for u in urls) if p}
    print(f"protected by recap snapshots: {len(paths):,} screenshot(s)")
    return paths


def partitions_to_scan(cutoff: datetime) -> list[int]:
    """Monthly partitions that can hold drops older than the cutoff."""
    rows = session.execute(text(
        "SELECT DISTINCT `partition` FROM drops "
        "WHERE `partition` IS NOT NULL AND `partition` <= :newest "
        "ORDER BY `partition`"
    ), {"newest": cutoff.year * 100 + cutoff.month}).all()
    return [int(r[0]) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prune low-value drop screenshots past the retention window.")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete files and clear image_url (default: dry run)")
    parser.add_argument("--min-value", type=int, default=DEFAULT_MIN_VALUE,
                        help=f"keep drops worth at least this much (default {DEFAULT_MIN_VALUE})")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"keep everything newer than this (default {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many candidate rows (0 = no limit)")
    args = parser.parse_args()

    cutoff = datetime.now() - timedelta(days=args.retention_days)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(REPO_ROOT, "logs", f"prune_drop_images_{stamp}.tsv")
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    mode = "APPLY (deleting)" if args.apply else "DRY RUN (nothing will be changed)"
    print(f"=== prune_drop_images — {mode} ===")
    print(f"keep if value*quantity >= {args.min_value:,} or newer than "
          f"{cutoff:%Y-%m-%d %H:%M} ({args.retention_days}d)")
    print(f"snapshot: {snapshot_path}\n")

    protected = recap_protected_paths()

    scanned = pruned = missing = unresolved = protected_hits = 0
    freed = 0
    pending_ids: list[int] = []
    stop = False

    clear_stmt = (
        text("UPDATE drops SET image_url = NULL WHERE drop_id IN :ids")
        .bindparams(bindparam("ids", expanding=True))
    )

    def flush_ids() -> None:
        """Clear image_url for the drops whose files we just handled."""
        if not (args.apply and pending_ids):
            pending_ids.clear()
            return
        for i in range(0, len(pending_ids), UPDATE_CHUNK):
            chunk = pending_ids[i:i + UPDATE_CHUNK]
            session.execute(clear_stmt, {"ids": chunk})
        session.commit()
        pending_ids.clear()

    with open(snapshot_path, "w", encoding="utf-8") as snap:
        # `action` distinguishes a real reclamation from clearing a reference
        # whose file was already gone — both change the DB, so both must be
        # recorded for the snapshot to actually be reversible.
        snap.write("drop_id\timage_url\tbytes\taction\n")
        for part in partitions_to_scan(cutoff):
            last_id = 0
            part_pruned = 0
            part_freed = 0
            while not stop:
                rows = session.execute(text(
                    "SELECT drop_id, image_url FROM drops "
                    "WHERE `partition` = :part AND drop_id > :last "
                    "  AND image_url IS NOT NULL AND image_url <> '' "
                    "  AND date_added < :cutoff "
                    "  AND (COALESCE(value,0) * COALESCE(quantity,1)) < :minv "
                    "ORDER BY drop_id LIMIT :lim"
                ), {"part": part, "last": last_id, "cutoff": cutoff,
                    "minv": args.min_value, "lim": SELECT_CHUNK}).all()
                if not rows:
                    break
                for drop_id, image_url in rows:
                    # Checked before the branches below: they `continue`, and a
                    # limit tested only on the delete path never fires on a run
                    # dominated by already-missing rows.
                    if args.limit and scanned >= args.limit:
                        stop = True
                        break
                    last_id = drop_id
                    scanned += 1
                    path = local_path_for(image_url)
                    if path is None:
                        unresolved += 1
                        continue
                    if path in protected:
                        # A recap card renders this one forever; leave both the
                        # file and the drops.image_url reference alone.
                        protected_hits += 1
                        continue
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        # Already gone — still clear the dangling reference
                        # (mostly pre-2026 rows in the retired npc/other
                        # layout, whose directories no longer exist).
                        missing += 1
                        snap.write(f"{drop_id}\t{image_url}\t0\tcleared_missing\n")
                        pending_ids.append(drop_id)
                        continue
                    snap.write(f"{drop_id}\t{image_url}\t{size}\tdeleted\n")
                    if args.apply:
                        try:
                            os.remove(path)
                        except OSError as exc:
                            print(f"  ! could not remove {path}: {exc}")
                            continue
                    pruned += 1
                    part_pruned += 1
                    freed += size
                    part_freed += size
                    pending_ids.append(drop_id)
                snap.flush()
                flush_ids()
            if part_pruned:
                print(f"  partition {part}: {part_pruned:,} images "
                      f"({_fmt_bytes(part_freed)})")
            if stop:
                break
        flush_ids()

    verb = "freed" if args.apply else "would free"
    print(f"\ncandidates scanned : {scanned:,}")
    print(f"images {'removed' if args.apply else 'to remove'} : {pruned:,}")
    print(f"space {verb}       : {_fmt_bytes(freed)}")
    if missing:
        print(f"already missing    : {missing:,} (image_url "
              f"{'cleared' if args.apply else 'would be cleared'})")
    if protected_hits:
        print(f"recap-protected    : {protected_hits:,} (kept for recap cards)")
    if unresolved:
        print(f"unrecognised urls  : {unresolved:,} (left untouched)")
    if not args.apply:
        print("\nDry run — nothing was changed. Re-run with --apply to act.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
