"""Retention for rendered character images (the gear pictures on PB posts).

``services/gear_image.py`` draws one 800x1200 PNG per outfit a player uploads,
so a personal-best notification can carry a picture of what they were wearing.
The model itself is bounded (``prune_old_models`` keeps 12 per player), but the
render never was: old Discord posts embed it by URL, so it was left alone, and
by the 2026-09-21 bucket census that was 1.23M renders / 98 GiB growing
~3.2 GiB a day, against 4.5 GiB of live models. Sixteen pictures for every
outfit we still hold, nearly all of outfits evicted long ago.

Policy: a render older than ``--retention-days`` (default 5) is deleted,
together with its derived ``-avatar.png`` crop, unless its fingerprint is one
the player still needs:

  * ``player_state.model_fingerprint``: what they are wearing now. The next
    personal best looks this one up, and the avatar on every leaderboard row
    is cut from it.
  * ``player_state.pinned_model_fingerprint``: the profile outfit they chose.
  * ``personal_best_loadouts.model_fingerprint``: outfits a record was set
    in, which the site shows for as long as the time stands.

That is the same protected set the model prune honours, so a protected
fingerprint keeps both its model and its picture. Anything else is a picture
of an outfit whose model is most likely gone already; the Discord post it
decorated has had its window, exactly as drop screenshots do (``prune_drop_
images``, also 5 days). A player who switches back into an older outfit gets
it re-rendered by ``POST /player/model/check``, from the model we kept.

B2 mode lists ``dt_img/models/`` once (~1.3M objects, a few minutes) and
deletes in DeleteObjects batches; local mode walks the models tree by mtime.
Both are gated the same way ``services.player_model`` decides where models
live, so the dev box prunes its local tree and never touches the bucket.

Safety:
  * Dry-run by default; ``--apply`` is required to delete anything.
  * Every removal is recorded to ``logs/prune_gear_renders_<ts>.tsv``
    (player_id, key, bytes) BEFORE it happens.
  * Only ``{fingerprint}.png`` / ``{fingerprint}-avatar.png`` under the
    models prefix are ever candidates; ``.glb`` models are never touched.
  * A failure to load the protected set aborts the run: with no protection,
    the current outfit of every player would be a candidate.

Run (dry-run):
    cd /store/droptracker/disc && venv/bin/python -m scripts.prune_gear_renders
Then, once the numbers look right:
    cd /store/droptracker/disc && venv/bin/python -m scripts.prune_gear_renders --apply

Scheduled daily via droptracker-prune-renders.timer.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_RETENTION_DAYS = 5
DELETE_CHUNK = 1_000

# ``{fingerprint}.png`` or ``{fingerprint}-avatar.png``; fingerprints are
# lowercase hex (services.player_model.is_valid_fingerprint).
_RENDER_RE = re.compile(r"^([0-9a-f]{1,32})(-avatar)?\.png$")


def _fmt_bytes(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < step:
            return f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TiB"


def parse_render_name(name: str) -> tuple[str, bool] | None:
    """``(fingerprint, is_avatar)`` for a render filename, else None."""
    m = _RENDER_RE.match(name)
    if not m:
        return None
    return m.group(1), bool(m.group(2))


def load_protected() -> dict[int, set[str]]:
    """``{player_id: {fingerprints}}`` that must keep their render.

    Raises on any failure rather than returning a partial set: a run with
    half the protection loaded would delete current outfits.
    """
    from db.models import PersonalBestEntry, PersonalBestLoadout, PlayerState, Session

    protected: dict[int, set[str]] = defaultdict(set)
    session = Session()
    try:
        for player_id, current, pinned in session.query(
            PlayerState.player_id,
            PlayerState.model_fingerprint,
            PlayerState.pinned_model_fingerprint,
        ):
            for fp in (current, pinned):
                if fp:
                    protected[int(player_id)].add(fp.lower())
        for player_id, fp in (
            session.query(PersonalBestEntry.player_id,
                          PersonalBestLoadout.model_fingerprint)
            .join(PersonalBestLoadout,
                  PersonalBestLoadout.pb_id == PersonalBestEntry.id)
            .filter(PersonalBestLoadout.model_fingerprint.isnot(None))
        ):
            if fp:
                protected[int(player_id)].add(fp.lower())
    finally:
        session.close()
    return dict(protected)


def sweep_b2(b2, cutoff: datetime, protected: dict[int, set[str]], snap,
             apply: bool, limit: int = 0) -> dict:
    """One listing of the models prefix; aged, unprotected renders go in
    batches. Returns counters."""
    prefix = b2.MODELS_PREFIX + "/"
    stats = {"scanned": 0, "candidates": 0, "removed": 0, "freed": 0,
             "protected": 0, "failed": 0}
    doomed: list[tuple[int, str, int]] = []  # player_id, key, size

    def flush() -> None:
        if not doomed:
            return
        failed = b2.delete_keys([k for _, k, _ in doomed]) if apply else set()
        for player_id, key, size in doomed:
            if key in failed:
                stats["failed"] += 1
                continue
            snap.write(f"{player_id}\t{key}\t{size}\n")
            stats["removed"] += 1
            stats["freed"] += size
        doomed.clear()

    for item in b2.list_keys(prefix):
        key = item["key"]
        rest = key[len(prefix):].split("/")
        if len(rest) != 2 or not rest[0].isdigit():
            continue
        parsed = parse_render_name(rest[1])
        if parsed is None:
            continue
        stats["scanned"] += 1
        fingerprint, _is_avatar = parsed
        player_id = int(rest[0])
        if fingerprint in protected.get(player_id, ()):
            stats["protected"] += 1
            continue
        uploaded = item.get("last_modified")
        if uploaded is None or uploaded >= cutoff:
            continue
        stats["candidates"] += 1
        doomed.append((player_id, key, int(item.get("size", 0))))
        if len(doomed) >= DELETE_CHUNK:
            flush()
        if limit and stats["candidates"] >= limit:
            break
    flush()
    return stats


def sweep_local(root: str, cutoff_ts: float, protected: dict[int, set[str]],
                snap, apply: bool, limit: int = 0) -> dict:
    """Filesystem twin of ``sweep_b2`` for installs without B2."""
    stats = {"scanned": 0, "candidates": 0, "removed": 0, "freed": 0,
             "protected": 0, "failed": 0}
    try:
        players = os.listdir(root)
    except OSError:
        return stats
    for entry in players:
        if not entry.isdigit():
            continue
        player_id = int(entry)
        directory = os.path.join(root, entry)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            parsed = parse_render_name(name)
            if parsed is None:
                continue
            stats["scanned"] += 1
            fingerprint, _is_avatar = parsed
            if fingerprint in protected.get(player_id, ()):
                stats["protected"] += 1
                continue
            path = os.path.join(directory, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime >= cutoff_ts:
                continue
            stats["candidates"] += 1
            snap.write(f"{player_id}\t{path}\t{st.st_size}\n")
            if apply:
                try:
                    os.unlink(path)
                except OSError as exc:
                    print(f"  ! could not remove {path}: {exc}")
                    stats["failed"] += 1
                    continue
            stats["removed"] += 1
            stats["freed"] += st.st_size
            if limit and stats["candidates"] >= limit:
                return stats
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete aged character renders (dry-run by default).")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: dry run)")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"keep renders newer than this (default {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many candidates (0 = no limit)")
    args = parser.parse_args()
    if args.retention_days < 1:
        print("--retention-days must be >= 1")
        return 2

    from services.player_model import MODEL_ROOT, _b2, _b2_enabled

    stamp = time.strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(REPO_ROOT, "logs", f"prune_gear_renders_{stamp}.tsv")
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    mode = "APPLY (deleting)" if args.apply else "DRY RUN (nothing will be changed)"
    backend = "B2" if _b2_enabled() else f"local ({MODEL_ROOT})"
    print(f"=== prune_gear_renders — {mode} — {backend} ===")
    print(f"keep renders newer than {args.retention_days}d, or of a protected outfit")
    print(f"snapshot: {snapshot_path}\n")

    try:
        protected = load_protected()
    except Exception as exc:
        print(f"could not load protected fingerprints, refusing to run: {exc}")
        return 1
    n_protected = sum(len(v) for v in protected.values())
    print(f"protected: {n_protected:,} fingerprints across {len(protected):,} players")

    started = time.time()
    with open(snapshot_path, "w", encoding="utf-8") as snap:
        snap.write("player_id\tkey\tbytes\n")
        if _b2_enabled():
            cutoff = datetime.now(timezone.utc) - timedelta(days=args.retention_days)
            stats = sweep_b2(_b2(), cutoff, protected, snap, args.apply, args.limit)
        else:
            cutoff_ts = time.time() - args.retention_days * 86400
            stats = sweep_local(MODEL_ROOT, cutoff_ts, protected, snap,
                                args.apply, args.limit)

    verb = "removed" if args.apply else "to remove"
    print(f"\nrenders scanned    : {stats['scanned']:,} ({time.time() - started:.0f}s)")
    print(f"protected          : {stats['protected']:,}")
    print(f"renders {verb:<10} : {stats['removed']:,}")
    print(f"space {'freed' if args.apply else 'would free'}        : {_fmt_bytes(stats['freed'])}")
    if stats["failed"]:
        print(f"failed             : {stats['failed']:,}")
    if not args.apply:
        print("\nDry run — nothing was changed. Re-run with --apply to act.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
