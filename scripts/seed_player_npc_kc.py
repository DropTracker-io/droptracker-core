"""Backfill player_npc_kc watermarks from historical drops.kill_count.

Without a baseline, the first post-deploy submission per (player, npc) seeds
the watermark SILENTLY (see data/submissions/kc_milestones.py) — correct, but
it means every existing grinder's next milestone is swallowed by the seed.
Folding drops history in first gives most active pairs a real baseline, so
crossings announce from day one, and largely defuses the false-first-kill edge
(a stray kc=1 from a divergent counter after a lost watermark).

drops has ~200M rows, so one big GROUP BY dies on the client read timeout —
this runs the repo's chunked-backfill idiom instead: binary-search the first
drop_id carrying a kill_count (the column is web76a-era; everything before it
is NULL), then sweep PK ranges of CHUNK ids, each as its own short statement.

Idempotent and resumable: ON DUPLICATE KEY UPDATE ... GREATEST() means
re-running any chunk (or the whole sweep) can only raise watermarks, and the
processors' own writes are never lowered.

Dry-run by default (reports the sweep plan + first-chunk sample); --apply to
write. --start-id resumes an interrupted sweep from a printed chunk boundary.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.seed_player_npc_kc [--apply] [--start-id N]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db.models import Session  # noqa: E402

#: PK ids per chunk. The engine's read_timeout is 30s (db/models/base.py); a
#: 1M-id chunk over the dense recent range (where nearly every drop carries a
#: KC) blew past it, so chunks are sized well under. A chunk that still times
#: out is retried in quarters — see _sweep_range.
CHUNK = 200_000

_PROBE_SQL = text(
    "SELECT 1 FROM drops WHERE drop_id >= :lo AND drop_id < :hi "
    "AND kill_count IS NOT NULL LIMIT 1"
)

_CHUNK_SQL = text(
    """
    INSERT INTO player_npc_kc (player_id, npc_id, kill_count)
    SELECT player_id, npc_id, MAX(kill_count)
    FROM drops
    WHERE drop_id >= :lo AND drop_id < :hi AND kill_count IS NOT NULL
    GROUP BY player_id, npc_id
    ON DUPLICATE KEY UPDATE
        kill_count = GREATEST(player_npc_kc.kill_count, VALUES(kill_count))
    """
)


def _has_kc(session, lo: int, hi: int) -> bool:
    return session.execute(_PROBE_SQL, {"lo": lo, "hi": hi}).first() is not None


def find_first_kc_drop_id(session, max_id: int) -> int | None:
    """Binary-search the earliest CHUNK whose id range carries any KC.

    Every probe is bounded to ONE chunk's PK range, so a no-KC chunk costs a
    ~1M-row range scan (about a second) and a KC chunk returns on its first
    hit — an unbounded LIMIT 1 over the whole table would scan the ~190M
    NULL-KC rows that precede web76a before finding anything. kill_count is
    contiguous-ish (NULL before web76a deployed, mostly present after), which
    is all a boundary search needs — the sweep still starts a chunk early for
    slack.
    """
    last_chunk = max_id // CHUNK
    if not _has_kc(session, last_chunk * CHUNK, max_id + 1):
        # Not even the newest drops carry KC — probe one chunk back before
        # concluding the column is empty (the tip chunk can be tiny).
        if last_chunk == 0 or not _has_kc(session, (last_chunk - 1) * CHUNK, last_chunk * CHUNK):
            return None
        last_chunk -= 1
    lo, hi = 0, last_chunk  # invariant: chunk `hi` has KC; chunks < lo don't
    while lo < hi:
        mid = (lo + hi) // 2
        if _has_kc(session, mid * CHUNK, (mid + 1) * CHUNK):
            hi = mid
        else:
            lo = mid + 1
    return max(0, (lo - 1) * CHUNK)  # one chunk of slack


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the watermarks (default: dry run)")
    parser.add_argument("--start-id", type=int, default=None,
                        help="resume the sweep from this drop_id (skips the binary search)")
    args = parser.parse_args()

    with Session() as session:
        max_id = session.execute(text("SELECT MAX(drop_id) FROM drops")).scalar() or 0
        start = args.start_id
        if start is None:
            start = find_first_kc_drop_id(session, max_id)
        if start is None:
            print("No drops carry a kill_count — nothing to backfill.")
            return
        chunks = (max_id - start) // CHUNK + 1
        print(f"Sweep plan: drop_id {start:,} .. {max_id:,} in {chunks} chunks of {CHUNK:,}")

        if not args.apply:
            print("DRY RUN — re-run with --apply to backfill player_npc_kc.")
            return

        total = 0
        swept = 0
        started = time.monotonic()
        lo = start
        while lo <= max_id:
            hi = lo + CHUNK
            total += _sweep_range(lo, hi)
            swept += 1
            if swept % 25 == 0 or hi > max_id:
                elapsed = time.monotonic() - started
                print(f"  chunk {swept}/{chunks} (drop_id <{hi:,}) — "
                      f"{total:,} rows affected, {elapsed:,.0f}s elapsed", flush=True)
            lo = hi
        pairs = session.execute(text("SELECT COUNT(*) FROM player_npc_kc")).scalar()
        print(f"APPLIED — {total:,} rows affected across {swept} chunks; "
              f"player_npc_kc now holds {pairs:,} pairs.")


def _sweep_range(lo: int, hi: int, depth: int = 0) -> int:
    """One range as its own session/statement; on a timeout, retry in quarters.

    A fresh session per range because a lost-connection error poisons the one
    it happened on. Gives up (re-raising) below 1k ids — at that size the
    problem is not chunk density.
    """
    from sqlalchemy.exc import OperationalError

    with Session() as s:
        try:
            result = s.execute(_CHUNK_SQL, {"lo": lo, "hi": hi})
            s.commit()
            return result.rowcount
        except OperationalError:
            s.rollback()
            if hi - lo <= 1_000 or depth >= 4:
                raise
    step = max(1_000, (hi - lo) // 4)
    print(f"  range {lo:,}..{hi:,} timed out — retrying in quarters", flush=True)
    affected = 0
    sub = lo
    while sub < hi:
        affected += _sweep_range(sub, min(sub + step, hi), depth + 1)
        sub += step
    return affected


if __name__ == "__main__":
    main()
