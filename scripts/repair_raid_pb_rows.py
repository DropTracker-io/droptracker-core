"""Delete raid personal_best rows poisoned FASTER than the player's real time.

Plugin versions before v5.4.1 could write two classes of impossibly-fast raid
PB rows, and a faster-than-truth row is permanent: both healing paths — the
``best_time`` down-sync in ``data/submissions/pb.py`` and the POH
adventure-log sync in ``data/submissions/adventure_log.py`` — only ever move a
row DOWN. A row faster than the player's genuine best therefore swallows every
real PB they set at that bracket, forever (verified live 2026-08-04: a real
42:38 ToA Expert PB silently discarded against a fake 40:00.0 row).

Classes removed:

* **target-capture** — ToA rows frozen at an exact whole minute. Plugin
  v5.3.0 (hub pin until 2026-08-03) had no filter for the "Your party
  beat/failed to beat the overall target time of 40:00!" messages, so the raid
  TARGET became the stored PB. Census: 421x 40:00, 207x 35:00, 108x 25:00,
  96x 30:00 — whole-minute values are ~8.7% of ToA rows vs a 0.9% legit
  baseline at non-raid bosses (and at ToB/CoX, which print no target lines and
  sit AT baseline, so they are excluded from this class). The ~1% of matches
  that are genuine whole-minute coincidences are recreated correctly by the
  next kill or POH sync (see below).

* **room-split** — ToB/ToA/CoX rows under 10:00, from room/wave duration
  lines captured as raid times (Verzik room, the Wardens, Olm). The slowest
  observed garbage is 5:26 ToA / 4:38 ToB / 9:59-with-39:27-kill CoX; the
  fastest LEGIT stored raid time is 12:20 (ToA Entry) / 12:40 (ToB) / 10:01
  (CoX), so a 10:00 cutoff clears both margins.

Deletion is safe and self-repairing: with no row present, ``pb_processor``'s
create branch stores ``min(kill_time, reported best)`` on the player's next
kill of that bracket — in-raid metric on plugin >= v5.4.1 — and a POH
adventure-log submission recreates the row from the game's own PB book.
Affected brackets simply show no PB until one of those happens.

Rows are dumped to ``logs/pb_repair_backup_<ts>.json`` before deletion.
``notified.pb_id`` (FK RESTRICT, nullable) is nulled for deleted ids first —
zero rows overlap today, this is a guard against races. Hall of Fame boards
pick the deletions up on their normal regeneration cycle. Seasonal tables use
the legacy leagues schema and are not touched. Idempotent: deleted rows no
longer match.

Also PRINTED (never deleted): CoX rows in 10:00-13:00 whose kill_time exceeds
twice the stored PB — the Olm-split poison signature above the hard cutoff —
for manual review.

Usage:
    venv/bin/python scripts/repair_raid_pb_rows.py            # dry run
    venv/bin/python scripts/repair_raid_pb_rows.py --apply
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db.models import session  # noqa: E402

FAST_CUTOFF_MS = 600_000  # 10:00 — see module docstring for the margins

TARGET_CAPTURE_SQL = """
    SELECT p.id FROM personal_best p
    JOIN npc_list n ON n.npc_id = p.npc_id
    WHERE n.npc_name LIKE 'Tombs of Amascut%'
      AND p.personal_best > 0
      AND MOD(p.personal_best, 60000) = 0
"""

ROOM_SPLIT_SQL = """
    SELECT p.id FROM personal_best p
    JOIN npc_list n ON n.npc_id = p.npc_id
    WHERE (n.npc_name LIKE 'Theatre of Blood%'
             OR n.npc_name LIKE 'Tombs of Amascut%'
             OR n.npc_name LIKE 'Chambers of Xeric%')
      AND p.personal_best > 0
      AND p.personal_best < :cutoff
"""

REVIEW_ONLY_SQL = """
    SELECT p.id, p.player_id, n.npc_name, p.team_size, p.personal_best,
           p.kill_time, p.date_added
    FROM personal_best p
    JOIN npc_list n ON n.npc_id = p.npc_id
    WHERE n.npc_name LIKE 'Chambers of Xeric%'
      AND p.personal_best >= :cutoff AND p.personal_best < 780000
      AND p.kill_time IS NOT NULL AND p.kill_time > 2 * p.personal_best
"""

DETAIL_SQL = """
    SELECT p.id, p.player_id, pl.player_name, n.npc_name, p.team_size,
           p.kill_time, p.personal_best, p.new_pb, p.image_url, p.date_added,
           p.used_api, p.unique_id, p.video_url, p.npc_id
    FROM personal_best p
    JOIN npc_list n ON n.npc_id = p.npc_id
    LEFT JOIN players pl ON pl.player_id = p.player_id
    WHERE p.id IN :ids
"""


def fmt_ms(v):
    if v is None:
        return "NULL"
    v = int(v)
    return f"{v // 60000}:{(v % 60000) / 1000:04.1f}"


def fetch_ids(sql, **params):
    return [r[0] for r in session.execute(text(sql), params).fetchall()]


def fetch_details(ids):
    if not ids:
        return []
    rows = []
    for chunk_start in range(0, len(ids), 500):
        chunk = tuple(ids[chunk_start:chunk_start + 500])
        rows.extend(session.execute(text(DETAIL_SQL), {"ids": chunk}).fetchall())
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete the rows (default: dry run)")
    args = parser.parse_args()

    target_ids = fetch_ids(TARGET_CAPTURE_SQL)
    split_ids = fetch_ids(ROOM_SPLIT_SQL, cutoff=FAST_CUTOFF_MS)
    split_only = sorted(set(split_ids) - set(target_ids))
    all_ids = sorted(set(target_ids) | set(split_ids))

    print(f"target-capture (ToA whole-minute): {len(target_ids)} rows")
    print(f"room-split (raid < {fmt_ms(FAST_CUTOFF_MS)}):       {len(split_only)} rows (excl. overlap)")
    print(f"TOTAL to delete:                   {len(all_ids)} rows")

    details = fetch_details(all_ids)
    by_npc = {}
    players = set()
    for d in details:
        by_npc[d[3]] = by_npc.get(d[3], 0) + 1
        players.add(d[1])
    print(f"distinct players affected:         {len(players)}")
    for npc, cnt in sorted(by_npc.items(), key=lambda kv: -kv[1]):
        print(f"    {npc:<38} {cnt}")

    print("\n--- candidate rows ---")
    for d in sorted(details, key=lambda d: (d[3], d[6])):
        (row_id, player_id, player_name, npc, team, kill, pb, _new_pb,
         img, added, _api, _uid, _vid, _npc_id) = d
        cls = "target " if row_id in set(target_ids) else "split  "
        print(f"  [{cls}] id={row_id:<7} {str(player_name)[:16]:<16} {npc:<36} "
              f"team={str(team):<5} pb={fmt_ms(pb):>7} kill={fmt_ms(kill):>7} added={added}")

    review = session.execute(text(REVIEW_ONLY_SQL), {"cutoff": FAST_CUTOFF_MS}).fetchall()
    if review:
        print("\n--- REVIEW ONLY (CoX 10:00-13:00 with kill > 2x pb — possible Olm"
              " splits above the cutoff; NOT deleted) ---")
        for row_id, player_id, npc, team, pb, kill, added in review:
            print(f"  id={row_id:<7} player={player_id:<9} {npc:<36} team={str(team):<5} "
                  f"pb={fmt_ms(pb):>7} kill={fmt_ms(kill):>7} added={added}")

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to delete.")
        return

    if not all_ids:
        print("\nNothing to delete.")
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", f"pb_repair_backup_{ts}.json",
    )
    with open(backup_path, "w") as fh:
        json.dump(
            [
                {
                    "id": d[0], "player_id": d[1], "player_name": d[2],
                    "npc_name": d[3], "npc_id": d[13], "team_size": d[4],
                    "kill_time": d[5], "personal_best": d[6], "new_pb": bool(d[7]),
                    "image_url": d[8], "date_added": str(d[9]),
                    "used_api": bool(d[10]) if d[10] is not None else None,
                    "unique_id": d[11], "video_url": d[12],
                    "class": "target" if d[0] in set(target_ids) else "split",
                }
                for d in details
            ],
            fh, indent=1,
        )
    print(f"\nbacked up {len(details)} rows to {backup_path}")

    deleted = 0
    for chunk_start in range(0, len(all_ids), 500):
        chunk = tuple(all_ids[chunk_start:chunk_start + 500])
        session.execute(
            text("UPDATE notified SET pb_id = NULL WHERE pb_id IN :ids"), {"ids": chunk}
        )
        deleted += session.execute(
            text("DELETE FROM personal_best WHERE id IN :ids"), {"ids": chunk}
        ).rowcount
    session.commit()
    print(f"deleted {deleted} personal_best rows. Rerun without --apply to verify 0 remain.")


if __name__ == "__main__":
    main()
