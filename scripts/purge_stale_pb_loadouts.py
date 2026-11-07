"""Remove personal-best loadouts that were written by a kill other than the PB.

Until bug #236 was fixed, ``data/submissions/pb._store_loadout`` stored the
gear of every timed kill onto the player's PB row for that boss, so a PB set
in June showed the Ascension crossbows its holder carried on a slow kill in
October. The submission path now writes gear only from the kill that set the
stored best; this clears what it wrote before.

There is no ``updated_at`` on ``personal_best_loadouts``, so an overwritten
row cannot be told apart from a genuine one by its own timestamps. Two tiers:

  * ``provable`` (default): the loadout row was CREATED more than ``--slack``
    seconds after the PB was set. That kill cannot have been the PB kill.
  * ``--include-later-kills``: also any loadout whose player has drops from
    that NPC more than ``--slack`` seconds after the PB was set
    (``player_npc_hourly_totals.last_drop_time``). The plugin sends gear on
    every timed kill, so such a row was almost certainly overwritten. It
    misses PBs whose drops land on a different NPC row, and it clears the
    rare row whose later kills happened to be in the same gear.

A removed loadout leaves the PB showing no gear, which is accurate: we do not
know what it was set in. The next genuine PB writes a fresh one.

Safety:
  * Dry-run by default; ``--apply`` is required to delete anything.
  * Every row is written to ``logs/purge_stale_pb_loadouts_<ts>.tsv`` (all
    columns) BEFORE it is deleted, so the run can be reversed.
  * Deletes in batches of ``BATCH`` by primary key.

Run (dry-run):
    cd /store/droptracker/disc && venv/bin/python -m scripts.purge_stale_pb_loadouts [--include-later-kills]
Then, once the numbers look right:
    cd /store/droptracker/disc && venv/bin/python -m scripts.purge_stale_pb_loadouts [--include-later-kills] --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH = 1_000
DEFAULT_SLACK_SECONDS = 120

_COLUMNS = ("pb_id", "equipment", "inventory", "model_fingerprint", "model_source", "created_at")


def candidate_sql(include_later_kills: bool) -> str:
    """Rows to remove: ``pb_id`` plus the loadout's columns and the PB's context."""
    later_kill = (
        """
        OR EXISTS (
            SELECT 1 FROM player_npc_hourly_totals t
            WHERE t.player_id = p.player_id AND t.npc_id = p.npc_id
              AND t.date_hour >= DATE_FORMAT(p.date_added, '%Y-%m-%d-%H')
              AND t.last_drop_time > p.date_added + INTERVAL :slack SECOND
        )"""
        if include_later_kills else ""
    )
    return f"""
        SELECT l.pb_id, l.equipment, l.inventory, l.model_fingerprint, l.model_source,
               l.created_at, p.player_id, p.npc_id, p.date_added
        FROM personal_best_loadouts l
        JOIN personal_best p ON p.id = l.pb_id
        WHERE l.created_at > p.date_added + INTERVAL :slack SECOND{later_kill}
    """


def _tsv(value) -> str:
    if value is None:
        return "\\N"
    return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def run(include_later_kills: bool, slack: int, apply: bool) -> int:
    from sqlalchemy import bindparam, text

    from db.models import Session

    session = Session()
    try:
        session.execute(text("SET SESSION max_statement_time=600"))
        rows = session.execute(
            text(candidate_sql(include_later_kills)), {"slack": slack}
        ).fetchall()
        total = session.execute(text("SELECT COUNT(*) FROM personal_best_loadouts")).scalar()
        players = len({r.player_id for r in rows})
        print(f"{len(rows):,} of {total:,} loadouts to remove "
              f"({players:,} players; tier={'later-kills' if include_later_kills else 'provable'}, "
              f"slack={slack}s)")
        if not rows:
            return 0

        os.makedirs(os.path.join(REPO_ROOT, "logs"), exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(REPO_ROOT, "logs", f"purge_stale_pb_loadouts_{stamp}.tsv")
        with open(path, "w") as snap:
            snap.write("\t".join(_COLUMNS + ("player_id", "npc_id", "pb_date_added")) + "\n")
            for r in rows:
                snap.write("\t".join(_tsv(getattr(r, c)) for c in _COLUMNS)
                           + f"\t{r.player_id}\t{r.npc_id}\t{_tsv(r.date_added)}\n")
        print(f"snapshot: {path}")

        if not apply:
            print("dry run: nothing deleted (pass --apply)")
            return len(rows)

        delete = text("DELETE FROM personal_best_loadouts WHERE pb_id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        ids = [r.pb_id for r in rows]
        removed = 0
        for i in range(0, len(ids), BATCH):
            result = session.execute(delete, {"ids": ids[i:i + BATCH]})
            session.commit()
            removed += result.rowcount or 0
        print(f"deleted {removed:,} loadouts")
        return removed
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--include-later-kills", action="store_true",
                        help="also remove loadouts whose player killed the NPC after the PB")
    parser.add_argument("--slack", type=int, default=DEFAULT_SLACK_SECONDS,
                        help="seconds after the PB still counted as the PB kill")
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args()
    run(args.include_later_kills, args.slack, args.apply)


if __name__ == "__main__":
    main()
