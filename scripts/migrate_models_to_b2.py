"""One-time move of the 3D model tree to B2 (then verified local erase).

The tree at ``static/assets/img/models/`` — models, their full-body renders
and avatar crops, ~341k files / 25 GiB as of 2026-08-30 — moves to the
``droptracker-video`` bucket under ``dt_img/models/`` with keys mirroring the
paths 1:1, and is then deleted locally. The services already read/write B2
when ``IMG_B2_OFFLOAD`` is on (see ``utils/image_storage.py``); this script
only has to make the existing files true there before the local copies go.

Three phases, each idempotent and resumable:

  (dry run)        census: what exists locally, what already matches in B2,
                   what would upload / what would be erased.
  --apply          upload every local file that is missing from B2 or differs
                   in size. Each upload is md5-vs-ETag verified by put_file;
                   a re-run only touches what the previous run did not finish.
  --delete-local   the erase pass (dry-run by default like everything else;
                   add --apply to actually erase). Re-lists the bucket fresh
                   and deletes ONLY local files whose B2 object exists with
                   the same size — byte-for-byte trust comes from the
                   upload-time md5 check, which this pass refuses to assume
                   for any file it cannot match. Mismatches are kept and
                   reported. Empty player directories are removed afterwards.

Every apply/delete action is recorded to ``logs/migrate_models_<ts>.tsv``
BEFORE it happens, mirroring scripts/prune_drop_images.py.

Only the four real artifact shapes are migrated (``{fp}.glb``,
``{fp}-pet.glb``, ``{fp}.png``, ``{fp}-avatar.png``); ``*.tmp`` writer
leftovers and anything unrecognised are skipped and counted, never uploaded,
never deleted.

Run from the repo root:
    venv/bin/python -m scripts.migrate_models_to_b2
    venv/bin/python -m scripts.migrate_models_to_b2 --apply
    venv/bin/python -m scripts.migrate_models_to_b2 --delete-local
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_ROOT = "/store/droptracker/disc/static/assets/img/models"

_ARTIFACT_RE = re.compile(r"^[0-9a-f]{1,32}(-pet)?(-avatar)?\.(glb|png)$")


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


def walk_local():
    """Yields ``(relpath, abspath, size)`` for every migratable artifact.

    ``relpath`` is ``{player_id}/{name}`` — exactly the key suffix under
    ``dt_img/models/``. Unrecognised names are counted, not yielded.
    """
    skipped = 0
    try:
        player_dirs = sorted(os.scandir(MODEL_ROOT), key=lambda e: e.name)
    except OSError:
        return
    for entry in player_dirs:
        if not entry.is_dir(follow_symlinks=False) or not entry.name.isdigit():
            skipped += 1
            continue
        try:
            files = os.scandir(entry.path)
        except OSError:
            continue
        with files:
            for f in files:
                if not f.is_file(follow_symlinks=False):
                    skipped += 1
                    continue
                if not _ARTIFACT_RE.match(f.name):
                    skipped += 1
                    continue
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                yield f"{entry.name}/{f.name}", f.path, size
    if skipped:
        print(f"  (skipped {skipped:,} non-artifact entries)")


def bucket_inventory(b2) -> dict[str, int]:
    """``{key suffix: size}`` for everything already under dt_img/models/."""
    prefix = b2.MODELS_PREFIX + "/"
    inventory: dict[str, int] = {}
    for item in b2.list_keys(prefix):
        inventory[item["key"][len(prefix):]] = item["size"]
    return inventory


def upload_pass(b2, todo, workers, snap) -> tuple[int, int, int]:
    uploaded = failed = 0
    uploaded_bytes = 0
    total = len(todo)
    started = time.monotonic()

    def _one(rel, path):
        key = f"{b2.MODELS_PREFIX}/{rel}"
        b2.put_file(key, path)
        return rel

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, rel, path): (rel, path, size)
                   for rel, path, size in todo}
        for done, future in enumerate(as_completed(futures), 1):
            rel, path, size = futures[future]
            try:
                future.result()
            except Exception as exc:
                failed += 1
                print(f"  ! upload failed {rel}: {exc}")
                snap.write(f"{rel}\t{size}\tupload_failed\n")
            else:
                uploaded += 1
                uploaded_bytes += size
                snap.write(f"{rel}\t{size}\tuploaded\n")
            if done % 2000 == 0 or done == total:
                rate = done / max(time.monotonic() - started, 0.001)
                print(f"  {done:,}/{total:,} "
                      f"({_fmt_bytes(uploaded_bytes)}, {rate:.0f}/s, "
                      f"{failed:,} failed)")
                snap.flush()
    return uploaded, uploaded_bytes, failed


def delete_pass(b2, local, snap, apply) -> tuple[int, int, int]:
    """Erase local files proven present in B2; returns (deleted, bytes, kept).

    The proof is a FRESH bucket listing (never the one the upload pass worked
    from) compared per file on size. put_file already verified content md5 at
    upload time; this pass verifies the object is still there and whole.
    """
    print("re-listing bucket for the erase pass…")
    inventory = bucket_inventory(b2)
    deleted = kept = 0
    deleted_bytes = 0
    for rel, path, size in local:
        remote = inventory.get(rel)
        if remote != size:
            kept += 1
            snap.write(f"{rel}\t{size}\tkept_"
                       f"{'missing' if remote is None else 'size_mismatch'}\n")
            continue
        snap.write(f"{rel}\t{size}\tdeleted_local\n")
        if apply:
            try:
                os.remove(path)
            except OSError as exc:
                print(f"  ! could not remove {path}: {exc}")
                kept += 1
                continue
        deleted += 1
        deleted_bytes += size
    snap.flush()

    if apply:
        removed_dirs = 0
        for entry in os.scandir(MODEL_ROOT):
            if entry.is_dir(follow_symlinks=False):
                try:
                    os.rmdir(entry.path)
                    removed_dirs += 1
                except OSError:
                    pass  # not empty — files were kept, or new writes landed
        if removed_dirs:
            print(f"removed {removed_dirs:,} empty player directories")
    return deleted, deleted_bytes, kept


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate the 3D model tree to B2, then erase it locally.")
    parser.add_argument("--apply", action="store_true",
                        help="upload missing/mismatched files (default: census only)")
    parser.add_argument("--delete-local", action="store_true",
                        help="verify against a fresh bucket listing and erase "
                             "matching local files")
    parser.add_argument("--workers", type=int, default=8,
                        help="concurrent uploads (default 8)")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop the upload list after N files (0 = all)")
    args = parser.parse_args()

    from utils import image_storage as b2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(REPO_ROOT, "logs", f"migrate_models_{stamp}.tsv")
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    mode = (f"DELETE-LOCAL {'(erasing)' if args.apply else '(dry run)'}"
            if args.delete_local
            else "APPLY (uploading)" if args.apply else "DRY RUN")
    print(f"=== migrate_models_to_b2 — {mode} ===")
    print(f"snapshot: {snapshot_path}\n")

    print("scanning local tree…")
    local = list(walk_local())
    local_bytes = sum(size for _, _, size in local)
    print(f"local artifacts: {len(local):,} files ({_fmt_bytes(local_bytes)})")

    with open(snapshot_path, "w", encoding="utf-8") as snap:
        snap.write("relpath\tbytes\taction\n")

        if args.delete_local:
            deleted, deleted_bytes, kept = delete_pass(
                b2, local, snap, apply=args.apply)
            verb = "erased locally" if args.apply else "would erase  "
            print(f"\n{verb} : {deleted:,} files "
                  f"({_fmt_bytes(deleted_bytes)})")
            if not args.apply:
                print("Dry run — nothing was deleted. "
                      "Re-run with --delete-local --apply to erase.")
            if kept:
                print(f"KEPT (not proven in B2): {kept:,} — run the upload "
                      f"--apply pass first, then this again")
                return 1
            return 0

        print("listing bucket…")
        inventory = bucket_inventory(b2)
        print(f"already in B2  : {len(inventory):,} objects")

        todo = [(rel, path, size) for rel, path, size in local
                if inventory.get(rel) != size]
        if args.limit:
            todo = todo[:args.limit]
        todo_bytes = sum(size for _, _, size in todo)
        print(f"to upload      : {len(todo):,} files ({_fmt_bytes(todo_bytes)})")

        if not args.apply:
            print("\nDry run — nothing was uploaded. Re-run with --apply.")
            return 0

        uploaded, uploaded_bytes, failed = upload_pass(
            b2, todo, args.workers, snap)
        print(f"\nuploaded : {uploaded:,} files ({_fmt_bytes(uploaded_bytes)})")
        if failed:
            print(f"FAILED   : {failed:,} — re-run --apply to retry them")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
