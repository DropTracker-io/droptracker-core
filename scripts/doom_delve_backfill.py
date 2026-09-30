"""Untangle Doom of Mokhaiotl personal bests and seed the deepest-delve record.

Before web124a / plugin 6.0.15 two kinds of Doom time landed on the boss row
(npc 14707) and ``personal_best`` kept whichever was faster:

* single-level splits from past level 8 ("Delve level: 8+ (9) duration: 1:27"),
  whose "8+" name the server could not parse, and
* the full "Delve level 1 - 8" run (10+ minutes), which is what the boss row is
  meant to hold.

A split is always faster, so most Doom boss rows (and every Doom Hall of Fame
PB board) show one deep level's time. The two never overlap: splits run under
3 minutes, full runs over 6.5. This script:

1. moves boss-row times under ``SPLIT_CUTOFF_MS`` to the 8+ row (14716), by
   changing ``npc_id`` so notification and loadout links keep pointing at the
   row. Where the player already has an 8+ row it reports and leaves both. A
   player whose 1 - 8 PB was hidden behind a split gets it back on their next
   full run: the game reports the standing PB on every line.
2. seeds ``player_deepest_delve`` from the rows that prove a completion:
   levels 1-8 exactly, an 8+ row as "9+" (the real level was never sent), a
   full run as 8. The upsert only ever raises a record, so re-runs and live
   intake are safe.

Main-world tables only. Dry run by default; ``--apply`` writes.

    ./venv/bin/python -m scripts.doom_delve_backfill
    ./venv/bin/python -m scripts.doom_delve_backfill --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import bindparam, text  # noqa: E402

from utils.doom_delve import DEEP_FLOOR, DEEP_NPC_ID, DOOM_NPC_ID, record_completed  # noqa: E402

SPLIT_CUTOFF_MS = 3 * 60 * 1000
FULL_RUN_MIN_MS = 6 * 60 * 1000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    from db.models.base import Session

    s = Session()
    try:
        if not s.execute(text("SELECT 1 FROM npc_list WHERE npc_id = :id"), {"id": DEEP_NPC_ID}).first():
            print(f"npc_list has no row {DEEP_NPC_ID}; run `alembic upgrade head` (web124a) first.")
            if args.apply:
                return 1
            print("(continuing the dry run anyway)")

        # ── 1. Split times off the boss row ─────────────────────────────────
        splits = s.execute(text(
            "SELECT b.id, b.player_id, b.team_size, b.personal_best, d.id AS deep_id "
            "FROM personal_best b "
            "LEFT JOIN personal_best d ON d.player_id = b.player_id AND d.npc_id = :deep "
            "  AND d.team_size = b.team_size "
            "WHERE b.npc_id = :doom AND b.personal_best > 0 AND b.personal_best < :cutoff"
        ), {"doom": DOOM_NPC_ID, "deep": DEEP_NPC_ID, "cutoff": SPLIT_CUTOFF_MS}).fetchall()
        movable = [r for r in splits if r.deep_id is None]
        clashes = [r for r in splits if r.deep_id is not None]
        print(f"8+ splits on the boss row: {len(splits)} "
              f"({len(movable)} to move, {len(clashes)} already have an 8+ row)")
        for r in clashes:
            print(f"  left in place: pb id {r.id} (player {r.player_id}); 8+ row is id {r.deep_id}")
        if args.apply and movable:
            s.execute(
                text("UPDATE personal_best SET npc_id = :deep WHERE id IN :ids AND npc_id = :doom")
                .bindparams(bindparam("ids", expanding=True)),
                {"deep": DEEP_NPC_ID, "doom": DOOM_NPC_ID, "ids": [r.id for r in movable]},
            )

        # ── 2. Deepest delve per player ─────────────────────────────────────
        # (player, level, exact, when) for every completion a row proves. On a
        # dry run the splits have not moved yet, so count them from where they
        # are now.
        evidence = s.execute(text(
            "SELECT player_id, npc_id - :doom AS level, 1 AS exact, MAX(date_added) AS at "
            "FROM personal_best WHERE npc_id BETWEEN :l1 AND :l8 AND personal_best > 0 "
            "GROUP BY player_id, npc_id "
            "UNION ALL "
            "SELECT player_id, :floor, 0, MAX(date_added) FROM personal_best "
            "WHERE (npc_id = :deep OR (npc_id = :doom AND personal_best > 0 AND personal_best < :cutoff)) "
            "GROUP BY player_id "
            "UNION ALL "
            "SELECT player_id, 8, 1, MAX(date_added) FROM personal_best "
            "WHERE npc_id = :doom AND personal_best >= :full GROUP BY player_id"
        ), {
            "doom": DOOM_NPC_ID, "deep": DEEP_NPC_ID, "l1": DOOM_NPC_ID + 1, "l8": DOOM_NPC_ID + 8,
            "floor": DEEP_FLOOR, "cutoff": SPLIT_CUTOFF_MS, "full": FULL_RUN_MIN_MS,
        }).fetchall()

        best: dict = {}
        for player_id, level, exact, at in evidence:
            key = (int(level), int(exact))
            if player_id not in best or key > best[player_id][:2]:
                best[player_id] = (int(level), int(exact), at)
        spread: dict = {}
        for level, exact, _ in best.values():
            label = f"{level}" if exact else f"{level}+"
            spread[label] = spread.get(label, 0) + 1
        print(f"deepest delve records: {len(best)} players")
        for label in sorted(spread, key=lambda v: (int(v.rstrip("+")), v)):
            print(f"  level {label}: {spread[label]}")

        if args.apply:
            for player_id, (level, exact, at) in best.items():
                record_completed(s, player_id, level, bool(exact), at)
            s.commit()
            print("applied.")
        else:
            s.rollback()
            print("dry run; nothing written (use --apply).")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
