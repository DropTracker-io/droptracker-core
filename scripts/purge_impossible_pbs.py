"""Delete personal-best rows that are faster than the known world records.

A short-lived game bug let players deal massively inflated damage, producing
raid completion times faster than the speedrunning world records. Anything
faster than the floors below (sourced from the speedrun Discord's records,
2026-07) is impossible and gets removed.

A row is impossible when its *effective* displayed time — ``personal_best``
or ``kill_time``, whichever positive value is smaller (matching how the site
picks the display time) — is below the floor for its boss + team size.

Boss families are resolved through ``utils.npc_names.npc_match_key`` so every
spelling/alias variant row is covered whether or not the duplicate-NPC merge
(scripts/merge_duplicate_npcs.py) has run yet.

Usage
-----
    python -m scripts.purge_impossible_pbs           # dry run (default)
    python -m scripts.purge_impossible_pbs --apply   # delete rows

Idempotent; prints every affected (player, team_size, time) in dry-run.
"""

import argparse
import sys
from collections import defaultdict

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from utils.npc_names import npc_match_key


def ms(minutes: int, seconds: float) -> int:
    return int(round((minutes * 60 + seconds) * 1000))


# match key -> {team_size (exact DB spelling): floor in ms}
FLOORS = {
    "chambers-of-xeric": {
        "Solo": ms(11, 4.2),
        "2": ms(9, 56.4),
    },
    "chambers-of-xeric-challenge-mode": {
        "Solo": ms(22, 37.8),
        "5": ms(15, 38.4),
    },
    "theatre-of-blood": {
        "Solo": ms(35, 31.8),
        "2": ms(18, 31.2),
        "3": ms(13, 16.8),
        "5": ms(11, 21.0),
    },
    "theatre-of-blood-hard-mode": {
        "Solo": ms(35, 31.8),
    },
    "tombs-of-amascut-expert-mode": {
        "Solo": ms(15, 52.8),
        "2": ms(15, 42.6),
        "3": ms(15, 37.8),
    },
}

# Effective displayed time: smallest positive of (personal_best, kill_time) —
# the same choice the site's display logic makes.
EFFECTIVE = (
    "CASE WHEN personal_best > 0 AND kill_time > 0 THEN LEAST(personal_best, kill_time) "
    "WHEN personal_best > 0 THEN personal_best ELSE COALESCE(kill_time, 0) END"
)


def fmt(msv: int) -> str:
    m, rem = divmod(int(msv), 60_000)
    return f"{m}:{rem // 1000:02d}.{(rem % 1000) // 100}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete rows (default: dry run)")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    s = Session()
    # Resolve every npc id belonging to each targeted family.
    ids_by_key = defaultdict(list)
    for nid, name in s.execute(text("SELECT npc_id, npc_name FROM npc_list")).fetchall():
        key = npc_match_key(name)
        if key in FLOORS:
            ids_by_key[key].append(int(nid))

    total = 0
    for key, floors in FLOORS.items():
        ids = ids_by_key.get(key)
        if not ids:
            print(f"[{mode}] {key}: no npc rows found — skipped")
            continue
        id_list = ",".join(map(str, ids))
        for team_size, floor in floors.items():
            rows = s.execute(text(
                f"SELECT pb.id, pb.player_id, p.player_name, {EFFECTIVE} AS eff "
                f"FROM personal_best pb LEFT JOIN players p ON p.player_id = pb.player_id "
                f"WHERE pb.npc_id IN ({id_list}) AND pb.team_size = :ts "
                f"HAVING eff > 0 AND eff < :floor ORDER BY eff ASC"
            ), {"ts": team_size, "floor": floor}).fetchall()
            if not rows:
                continue
            print(f"[{mode}] {key} {team_size}: {len(rows)} rows faster than {fmt(floor)}")
            for row_id, pid, pname, eff in rows:
                print(f"    pb#{row_id} {pname or pid}: {fmt(eff)}")
                if args.apply:
                    s.execute(text("DELETE FROM personal_best WHERE id = :i"), {"i": row_id})
            total += len(rows)

    if args.apply:
        s.commit()
    s.close()
    print(f"\n[{mode}] {total} impossible PB rows {'deleted' if args.apply else 'found'}.")


if __name__ == "__main__":
    main()
