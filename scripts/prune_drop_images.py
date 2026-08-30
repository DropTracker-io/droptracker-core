"""Retention for user-uploaded submission screenshots (storage reclamation).

Covers ALL nine submission types under ``static/assets/img/user-upload/``, by
two different mechanisms, because the types differ in one decisive way:

  * ``drop`` has a *value*, so it is driven from ``drops.image_url`` and keeps
    anything worth >= ``--min-value`` regardless of age (see below).
  * the other eight (``level_up``, ``clog``, ``ca``, ``pb``, ``quest``,
    ``pet``, ``death``, ``experience_milestone``) have no value to weigh and
    are swept off the *filesystem* by mtime.

Why the second mechanism had to exist (2026-08-19): this script originally
scanned only ``drops``, so it only ever saw ``drop/``. The other eight types
had no retention at all and grew to 96 GiB — ``level_up`` alone reached 223k
files / 42.5 GiB, referenced by no table whatsoever (``player_exp`` has no
image column; those files exist purely to be embedded in one Discord
notification). The disk hit 99% while this script ran green every night,
reporting a healthy 1.9 GiB reclaimed. A DB-driven scan structurally cannot
see a file nothing in the DB points at — hence the filesystem sweep.

Note both mechanisms honour the same recap protection, and deleting any image
blanks it in Discord messages older than the retention window: the URL lives
in the message, and there is no backup of this tree anywhere.

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
from it.

**How the scan is chunked, and why it must stay that way.** Day-sized
``date_added`` ranges, ordered by ``(date_added, drop_id)``, riding
``ix_drops_date_added``. This is not a stylistic choice — it is the same rule
``services/recap.py`` documents at length, and this script was killed by
breaking it (2026-08-01: "Lost connection to MySQL server during query"). The
two ways to get it wrong, both measured with EXPLAIN on the live 175M-row
table:

* ``WHERE partition = :p`` — what this used to do. There is no composite of
  ``partition`` with anything, so it degrades to a ``ref`` over the *entire*
  partition: 33.3M rows for 202607, which is a timeout, not a query.
* ``ORDER BY drop_id`` over a ``date_added`` range — the optimiser abandons the
  date index in favour of the PRIMARY key to satisfy the sort, then walks the
  table from row 1: 87.8M rows estimated, worse than what it replaced.

Ordering by ``date_added`` keeps the range on the index it was given: **776k
rows**, ~0.5s per day. Keyset pagination therefore has to be expressed against
``(date_added, drop_id)`` rather than ``drop_id`` alone.

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
DEFAULT_NONDROP_RETENTION_DAYS = 30

# Every submission type that lands in the user-upload tree, and the table whose
# ``image_url`` points at it (None = nothing in the DB references these files).
#
# `drop` is deliberately absent: it is the one type with a *value* to weigh, so
# it keeps the DB-driven scan below. The rest are swept off the filesystem —
# see `prune_non_drop_images` for why that direction is the only one that works.
NON_DROP_TYPES = {
    "level_up": None,
    "pet": None,
    "experience_milestone": None,
    "clog": ("collection", "log_id"),
    "ca": ("combat_achievement", "id"),
    "pb": ("personal_best", "id"),
    "quest": ("quest_completions", "id"),
    "death": ("player_deaths", "id"),
}
# Rows per window query / ids per UPDATE. Keeps each statement well inside
# MariaDB's read_timeout and bounds memory on a 175M-row table.
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


def _b2_storage():
    """The image-storage module, or None when B2 is not usable here.

    Gated on credentials being present rather than on ``IMG_B2_OFFLOAD``:
    B2-hosted rows exist iff the offload ever ran, and they must keep aging
    out even if the flag is later turned off. Without credentials every
    B2-hosted URL is treated as unresolved — a reference this script cannot
    verify is a reference it must not touch.
    """
    try:
        # Imported before the env check on purpose: utils.b2_storage runs
        # load_dotenv() at import, and this script may be the first thing in
        # the process to need those keys.
        from utils import image_storage
    except Exception:
        return None
    if not os.getenv("B2_KEY_ID"):
        return None
    return image_storage


def b2_key_for(image_url: str):
    """B2 object key for a CDN-hosted screenshot URL, or None.

    Restricted to the user-upload namespace the same way ``local_path_for``
    is restricted to the user-upload root: a row pointing anywhere else in
    the bucket (a model, a video) must never become deletable from here.
    """
    b2 = _b2_storage()
    if b2 is None:
        return None
    key = b2.key_from_url(image_url)
    if key and key.startswith(b2.USER_UPLOAD_PREFIX + "/"):
        return key
    return None


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


def recap_protected_paths() -> tuple[set[str], set[str]]:
    """Screenshots a recap card points at, which must survive pruning —
    returned as ``(local paths, B2 keys)`` since uploads live in both places.

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
    b2_keys = {k for k in (b2_key_for(u) for u in urls) if k}
    print(f"protected by recap snapshots: {len(paths):,} local + "
          f"{len(b2_keys):,} B2 screenshot(s)")
    return paths, b2_keys


def windows_to_scan(cutoff: datetime) -> list[tuple[datetime, datetime]]:
    """Day-sized ``[start, end)`` windows covering every drop older than the
    cutoff, oldest first.

    Half-open so a drop at 23:59:59.999 lands in exactly one window, and sized
    by day rather than by month because the window bounds *are* the index range
    this scan rides on — see the module docstring. A day is ~300k rows, which
    reads in well under a second.
    """
    # No WHERE clause on purpose. `MIN()` already ignores NULLs, and a bare
    # MIN over an indexed column is resolved by a single index seek ("Select
    # tables optimized away"); adding `WHERE date_added IS NOT NULL` defeats
    # that and makes it range-scan all 87.8M index entries — measured, it times
    # out exactly like the query this rewrite exists to fix.
    earliest = session.execute(text("SELECT MIN(date_added) FROM drops")).scalar()
    if earliest is None:
        return []
    windows = []
    start = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
    while start < cutoff:
        windows.append((start, min(start + timedelta(days=1), cutoff)))
        start += timedelta(days=1)
    return windows


def iter_type_files(submission_type: str):
    """Yield ``(path, stat)`` for every file under ``*/{submission_type}/``.

    ``scandir`` rather than ``glob``/``walk`` because this crosses ~1.1M files
    and ``scandir`` carries the stat result with the entry, so the age test
    below costs no extra syscall.
    """
    try:
        wom_dirs = os.scandir(LOCAL_ROOT)
    except OSError:
        return
    with wom_dirs:
        for wom in wom_dirs:
            if not wom.is_dir(follow_symlinks=False):
                continue
            type_dir = os.path.join(wom.path, submission_type)
            if not os.path.isdir(type_dir):
                continue
            for root, _dirs, files in os.walk(type_dir):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        yield path, os.stat(path)
                    except OSError:
                        continue


def prune_non_drop_images(cutoff, protected, snap, apply, retention_days):
    """Delete non-drop submission screenshots past the retention window.

    **Driven from the filesystem, not the DB — and that is the whole point.**
    `level_up`, `pet` and `experience_milestone` are referenced by *no* table:
    they are written, embedded once in a Discord notification, and never read
    again. A DB-driven scan cannot see them, which is exactly how `level_up`
    reached 223k files / 42.5 GiB while the nightly drop prune ran green every
    morning. Walking the tree covers all eight types uniformly; the references
    that *do* exist are healed afterwards by `heal_missing_references`.

    Age comes from mtime because these types have no value to weigh — the
    retention window is the entire policy.
    """
    totals = {}
    for submission_type in sorted(NON_DROP_TYPES):
        removed = freed = skipped = 0
        for path, st in iter_type_files(submission_type):
            if datetime.fromtimestamp(st.st_mtime) >= cutoff:
                continue
            if path in protected:
                # A recap card renders this one forever.
                skipped += 1
                continue
            # Recorded BEFORE the unlink, same as the drop path, so the log is
            # a complete account of what went even if the run dies mid-sweep.
            snap.write(f"0\t{path}\t{st.st_size}\tdeleted_{submission_type}\n")
            if apply:
                try:
                    os.remove(path)
                except OSError as exc:
                    print(f"  ! could not remove {path}: {exc}")
                    continue
            removed += 1
            freed += st.st_size
        snap.flush()
        if removed or skipped:
            note = f" ({skipped:,} recap-protected)" if skipped else ""
            print(f"  {submission_type:<22}: {removed:,} images "
                  f"({_fmt_bytes(freed)}){note}")
        totals[submission_type] = (removed, freed)
    return totals


def prune_b2_refless_images(retention_days, protected_b2, snap, apply):
    """Sweep B2-hosted screenshots of the reference-less types by upload time.

    The same fact that forced the filesystem sweep — ``level_up``/``pet``/
    ``experience_milestone`` files are referenced by no table — applies in the
    bucket: nothing DB-driven can ever see them. One listing of the
    user-upload prefix stands in for the directory walk; the bucket key's
    second path segment is the submission type, and LastModified is the upload
    time (objects are write-once, so it is exactly the local mtime's twin).

    Referenced types are deliberately skipped here — their retention is
    decided row-by-row in ``heal_missing_references`` (and ``drop`` by value
    in the main scan) — as is anything with an unrecognised layout.
    """
    b2 = _b2_storage()
    if b2 is None:
        return {}
    from datetime import timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    refless = {t for t, ref in NON_DROP_TYPES.items() if ref is None}
    totals = {t: [0, 0, 0] for t in refless}  # removed, freed, skipped
    prefix = b2.USER_UPLOAD_PREFIX + "/"
    try:
        listing = b2.list_keys(prefix)
        for item in listing:
            key = item["key"]
            parts = key[len(prefix):].split("/")
            if len(parts) < 3 or parts[1] not in refless:
                continue
            submission_type = parts[1]
            uploaded = item.get("last_modified")
            if uploaded is None or uploaded >= cutoff:
                continue
            if key in protected_b2:
                totals[submission_type][2] += 1
                continue
            snap.write(f"0\t{key}\t{item['size']}\tdeleted_b2_{submission_type}\n")
            if apply and not b2.delete_key(key):
                continue
            totals[submission_type][0] += 1
            totals[submission_type][1] += item["size"]
    except Exception as exc:
        print(f"  ! B2 listing failed, sweep incomplete: {exc}")
    snap.flush()
    for submission_type in sorted(refless):
        removed, freed, skipped = totals[submission_type]
        if removed or skipped:
            note = f" ({skipped:,} recap-protected)" if skipped else ""
            print(f"  {submission_type:<22}: {removed:,} B2 objects "
                  f"({_fmt_bytes(freed)}){note}")
    return totals


def heal_missing_references(cutoff, apply, protected_b2=frozenset(), snap=None):
    """Null ``image_url`` on rows whose file is gone, so the site renders "no
    screenshot" instead of a broken image — and, for B2-hosted rows past the
    cutoff, delete the object first.

    The B2 half is not healing but *retention*: the filesystem sweep that ages
    these types out structurally cannot see a bucket object, and this keyset
    walk over the referencing tables is already visiting exactly the rows that
    have aged past the window, so the deletion lives here rather than in a
    second identical walk. Local rows keep the original contract: only ever
    null a reference whose file is already absent.

    These tables are 30k-290k rows (against drops' 188M), so a plain keyset
    walk on the primary key is fine here — none of the index gymnastics the
    drops scan needs.
    """
    cleared_total = 0
    for submission_type, ref in sorted(NON_DROP_TYPES.items()):
        if ref is None:
            continue
        table, pk = ref
        cleared = 0
        last_pk = -1
        while True:
            rows = session.execute(text(
                f"SELECT {pk}, image_url FROM {table} "
                f"WHERE {pk} > :last_pk AND date_added < :cutoff "
                "  AND image_url IS NOT NULL AND image_url <> '' "
                f"ORDER BY {pk} LIMIT :lim"
            ), {"last_pk": last_pk, "cutoff": cutoff,
                "lim": SELECT_CHUNK}).all()
            if not rows:
                break
            stale = []
            for row_pk, image_url in rows:
                last_pk = row_pk
                path = local_path_for(image_url)
                if path is not None:
                    if os.path.exists(path):
                        continue
                    stale.append(row_pk)
                    continue
                b2key = b2_key_for(image_url)
                if b2key is None or b2key in protected_b2:
                    continue
                if snap is not None:
                    snap.write(f"{row_pk}\t{image_url}\t0\t"
                               f"deleted_b2_{submission_type}\n")
                if apply:
                    b2 = _b2_storage()
                    if not b2.delete_key(b2key):
                        # Keep the reference if the object could not be
                        # removed — a dangling delete would orphan it forever.
                        continue
                stale.append(row_pk)
            if stale and apply:
                stmt = (text(f"UPDATE {table} SET image_url = NULL "
                             f"WHERE {pk} IN :ids")
                        .bindparams(bindparam("ids", expanding=True)))
                for i in range(0, len(stale), UPDATE_CHUNK):
                    session.execute(stmt, {"ids": stale[i:i + UPDATE_CHUNK]})
                session.commit()
            cleared += len(stale)
        if cleared:
            print(f"  {table:<22}: {cleared:,} dangling image_url "
                  f"{'cleared' if apply else 'would be cleared'}")
        cleared_total += cleared
    return cleared_total


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
    parser.add_argument("--nondrop-retention-days", type=int,
                        default=DEFAULT_NONDROP_RETENTION_DAYS,
                        help="retention window for non-drop submission types "
                             f"(default {DEFAULT_NONDROP_RETENTION_DAYS}); "
                             "these have no value to weigh, so age is the "
                             "whole policy")
    parser.add_argument("--skip-non-drop", action="store_true",
                        help="only prune drop screenshots (legacy behaviour)")
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

    protected, protected_b2 = recap_protected_paths()

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
        for win_start, win_end in windows_to_scan(cutoff):
            last_at, last_id = win_start, -1
            part_pruned = 0
            part_freed = 0
            while not stop:
                rows = session.execute(text(
                    "SELECT drop_id, image_url, date_added FROM drops "
                    "WHERE date_added >= :win_start AND date_added < :win_end "
                    # Keyset pagination on the same (date_added, drop_id) order
                    # the rows come back in. Expressed against date_added rather
                    # than drop_id alone so the optimiser keeps the date index —
                    # `ORDER BY drop_id` makes it switch to the PRIMARY key and
                    # walk the table from row 1 (see the note above).
                    "  AND (date_added > :last_at "
                    "       OR (date_added = :last_at AND drop_id > :last_id)) "
                    "  AND image_url IS NOT NULL AND image_url <> '' "
                    "  AND (COALESCE(value,0) * COALESCE(quantity,1)) < :minv "
                    "ORDER BY date_added, drop_id LIMIT :lim"
                ), {"win_start": win_start, "win_end": win_end,
                    "last_at": last_at, "last_id": last_id,
                    "minv": args.min_value, "lim": SELECT_CHUNK}).all()
                if not rows:
                    break
                for drop_id, image_url, date_added in rows:
                    # Checked before the branches below: they `continue`, and a
                    # limit tested only on the delete path never fires on a run
                    # dominated by already-missing rows.
                    if args.limit and scanned >= args.limit:
                        stop = True
                        break
                    last_at, last_id = date_added, drop_id
                    scanned += 1
                    path = local_path_for(image_url)
                    if path is None:
                        # Not a local file — a B2-hosted screenshot gets the
                        # same treatment (delete object, clear reference);
                        # anything else is left strictly alone.
                        b2key = b2_key_for(image_url)
                        if b2key is None:
                            unresolved += 1
                            continue
                        if b2key in protected_b2:
                            protected_hits += 1
                            continue
                        b2 = _b2_storage()
                        try:
                            info = b2.head(b2key)
                        except Exception as exc:
                            print(f"  ! could not check {b2key}: {exc}")
                            continue
                        if info is None:
                            missing += 1
                            snap.write(f"{drop_id}\t{image_url}\t0\tcleared_missing\n")
                            pending_ids.append(drop_id)
                            continue
                        snap.write(f"{drop_id}\t{image_url}\t{info['size']}\tdeleted_b2\n")
                        if args.apply and not b2.delete_key(b2key):
                            continue
                        pruned += 1
                        part_pruned += 1
                        freed += info["size"]
                        part_freed += info["size"]
                        pending_ids.append(drop_id)
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
                print(f"  {win_start:%Y-%m-%d}: {part_pruned:,} images "
                      f"({_fmt_bytes(part_freed)})")
            if stop:
                break
        flush_ids()

        if not args.skip_non_drop:
            nd_cutoff = datetime.now() - timedelta(
                days=args.nondrop_retention_days)
            print(f"\nnon-drop submission types (older than "
                  f"{nd_cutoff:%Y-%m-%d %H:%M}, "
                  f"{args.nondrop_retention_days}d):")
            nd_totals = prune_non_drop_images(
                nd_cutoff, protected, snap, args.apply,
                args.nondrop_retention_days)
            nd_removed = sum(n for n, _ in nd_totals.values())
            nd_freed = sum(f for _, f in nd_totals.values())
            if _b2_storage() is not None:
                print(f"\nB2-hosted non-drop types (older than "
                      f"{args.nondrop_retention_days}d):")
                b2_totals = prune_b2_refless_images(
                    args.nondrop_retention_days, protected_b2, snap,
                    args.apply)
                nd_removed += sum(t[0] for t in b2_totals.values())
                nd_freed += sum(t[1] for t in b2_totals.values())
            print(f"\nhealing dangling references:")
            nd_cleared = heal_missing_references(
                nd_cutoff, args.apply, protected_b2, snap)

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
        # Overwhelmingly rows with image_url = '' — a drop that simply carried
        # no screenshot. Nothing to reclaim; they are not a backlog.
        print(f"unrecognised urls  : {unresolved:,} (left untouched)")
    if not args.skip_non_drop:
        print(f"\nnon-drop images {'removed' if args.apply else 'to remove'} "
              f": {nd_removed:,}")
        print(f"non-drop space {verb}  : {_fmt_bytes(nd_freed)}")
        if nd_cleared:
            print(f"references healed     : {nd_cleared:,}")
        print(f"\nTOTAL space {verb}     : {_fmt_bytes(freed + nd_freed)}")
    if not args.apply:
        print("\nDry run — nothing was changed. Re-run with --apply to act.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
