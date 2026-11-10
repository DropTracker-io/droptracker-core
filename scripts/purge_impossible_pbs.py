"""Delete personal-best rows that are faster than the known world records.

A short-lived game bug let players deal massively inflated damage, producing
raid completion times faster than the speedrunning world records. Anything
faster than the floors below (sourced from the speedrun Discord's records,
2026-07) is impossible and gets removed.

Every raid mode with an intake floor in ``utils.pb_floors`` is checked
against that floor too, on every team size: a time under it is a single room
or wave filed as the whole raid (ticket 521), which the floors below miss on
the team sizes they do not list.

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

Idempotent; prints every affected (player, team_size, time) in dry-run, and
writes the rows to logs/ before anything is deleted. A time just under a
record (``--margin``, default 2%) is listed for review, never deleted.
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import bindparam, text

from db import Session
from utils.npc_names import npc_match_key
from utils.pb_floors import FLOORS_MS


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

ROW_COLUMNS = (
    "id, player_id, npc_id, team_size, kill_time, personal_best, new_pb, "
    "image_url, date_added, used_api, unique_id"
)


def fmt(msv: int) -> str:
    m, rem = divmod(int(msv), 60_000)
    return f"{m}:{rem // 1000:02d}.{(rem % 1000) // 100}"


def classify(key, team_size, eff, margin):
    """Why a row is impossible, or why it is kept for review, or None.

    Returns ``("delete", reason)``, ``("review", reason)`` or ``None``. A time
    under the intake floor (``utils.pb_floors``) is a room or wave time, never
    a run. A time under a team size's world record is impossible too, unless
    it is within ``margin`` of the record: that may be a new record, or the
    record list may be out of date, so it is printed rather than deleted.
    """
    floor = FLOORS_MS.get(key)
    if floor is not None and 0 < eff < floor:
        return "delete", f"under the {fmt(floor)} intake floor"
    record = FLOORS.get(key, {}).get(team_size)
    if record is not None and 0 < eff < record:
        if eff >= record * (1 - margin):
            return "review", f"within {margin:.0%} of the {fmt(record)} record"
        return "delete", f"faster than the {fmt(record)} record"
    return None


def delete_rows(s, ids):
    """Drop PB rows and what hangs off them (both foreign keys are RESTRICT)."""
    for i in ids:
        s.execute(text("UPDATE notified SET pb_id = NULL WHERE pb_id = :i"), {"i": i})
        s.execute(text("DELETE FROM personal_best_loadouts WHERE pb_id = :i"), {"i": i})
        s.execute(text("DELETE FROM personal_best WHERE id = :i"), {"i": i})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete rows (default: dry run)")
    parser.add_argument("--margin", type=float, default=0.02,
                        help="keep times this close under a record for review (default 0.02)")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    s = Session()
    try:
        # Resolve every npc id belonging to each targeted family.
        keys = set(FLOORS) | set(FLOORS_MS)
        key_by_id = {}
        for nid, name in s.execute(text("SELECT npc_id, npc_name FROM npc_list")).fetchall():
            key = npc_match_key(name)
            if key in keys:
                key_by_id[int(nid)] = key
        if not key_by_id:
            print(f"[{mode}] no npc rows found")
            return

        cols = [c.strip() for c in ROW_COLUMNS.split(",")]
        id_list = ",".join(map(str, key_by_id))
        rows = [dict(zip(cols + ["eff"], r)) for r in s.execute(text(
            f"SELECT {ROW_COLUMNS}, {EFFECTIVE} AS eff FROM personal_best "
            f"WHERE npc_id IN ({id_list}) ORDER BY npc_id, team_size, eff"
        )).fetchall()]

        names = dict(s.execute(text(
            "SELECT player_id, player_name FROM players WHERE player_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
            {"ids": list({r["player_id"] for r in rows}) or [-1]}).fetchall())

        junk, review = [], []
        for r in rows:
            verdict = classify(key_by_id[r["npc_id"]], r["team_size"], r["eff"] or 0, args.margin)
            if verdict is None:
                continue
            kind, reason = verdict
            r["key"], r["reason"] = key_by_id[r["npc_id"]], reason
            (junk if kind == "delete" else review).append(r)

        for title, group in (("impossible", junk), ("kept for review", review)):
            print(f"[{mode}] {len(group)} rows {title}")
            for r in group:
                who = names.get(r["player_id"], r["player_id"])
                print(f"    pb#{r['id']} {r['key']} {r['team_size']} {who}: "
                      f"{fmt(r['eff'])} ({r['reason']})")

        os.makedirs("logs", exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = f"logs/purge_impossible_pbs_{'apply' if args.apply else 'dryrun'}_{stamp}.json"
        with open(path, "w") as fh:
            json.dump({"deleted": junk, "review": review}, fh, indent=1, default=str)
        print(f"[{mode}] backup: {path}")

        if args.apply:
            delete_rows(s, [r["id"] for r in junk])
            s.commit()
        else:
            s.rollback()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()
    print(f"\n[{mode}] {len(junk)} impossible PB rows {'deleted' if args.apply else 'found'}. "
          "Boss-record badges move on the next badge pass.")


if __name__ == "__main__":
    main()
