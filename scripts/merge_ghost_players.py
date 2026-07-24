#!/usr/bin/env python3
"""Merge WOM-import "ghost" player rows into their real (plugin-authed) twin.

Background: a WOM clan-roster import (utils/wiseoldman.py _create_player_from_wom_member)
minted a duplicate players row (account_hash 'wom_temp_<wom_id>') for an account that
already existed under a real account_hash, because the lookup was an exact-string name
match (missing "lm_Brad" vs "lm Brad") and/or the account carried a second WOM id. The
clan memberships attached to the ghost, so the real row (which actually receives drops)
never got notified for those clans. See memory brondt-crimson-kisten-notify-investigation.

This tool reassigns every players.player_id reference from the ghost to the real row
(with unique-key dedup), rewrites the ghost id embedded in WOM event-completion guids,
asserts the ghost has zero remaining references, then deletes the ghost. Idempotent:
once a ghost is gone, re-running its pair is a no-op.

SAFETY:
  * Dry-run by default — runs every statement inside a transaction then ROLLS BACK,
    reporting exactly what would change (and surfacing any constraint error) without
    persisting. Pass --apply to COMMIT.
  * Deploy the utils/wiseoldman.py normalized-lookup fix FIRST, or a later WOM sync
    can re-mint the ghost.
  * Only rows whose account_hash starts with 'wom_temp_' are ever deleted.

Usage:
  python scripts/merge_ghost_players.py                 # dry-run, all configured pairs
  python scripts/merge_ghost_players.py --only 5754005 5755171   # dry-run, subset
  python scripts/merge_ghost_players.py --apply --only 5754005 5755171   # APPLY subset
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pymysql
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# (ghost_player_id, real_player_id, real_user_id_or_None, adopt_wom_id_or_None, label)
#
# adopt_wom_id: the WOM id the REAL row should end up carrying. Set it when the
# real row's stored wom_id is dead on WOM's side while the ghost carries the id
# the clan rosters actually report — leaving the stale id in place is what makes
# the account re-ghost (see merge_pair step 8). None leaves players.wom_id alone.
PAIRS = [
    # --- already merged 2026-07-23; kept for provenance, re-running them is a no-op ---
    (5755309, 3, 3, None, "Brondt (ghost wom 169385 -> real 3, Discord brondt_)"),
    (5754005, 5754170, None, None, "solo novelli (ghost wom 2606248 -> real 5754170)"),
    (5755171, 5753485, None, None, "lm_Brad -> lm Brad (space/underscore, ghost wom 3170694 -> real 5753485)"),
    # --- 2026-07-24: the two remaining split pairs, both stale-wom_id re-ghosts ---
    # Brondt re-ghosted 2026-07-23 13:23 despite the create-time fix. Real row 3 was
    # pinned to wom 796802, which WOM resolves to an empty "unknown" record (0 exp,
    # combat level 3, no snapshot since 2022) — so the roster sync never matched him
    # by id. The ghost carries the live account (747M exp, updated daily).
    (5755906, 3, 3, 169385, "Brondt (ghost wom 169385 -> real 3; real held dead wom 796802)"),
    # Same shape: real row 5755813 holds wom 3259035, which WOM 404s outright.
    (5755787, 5755813, None, 3145894, "Tril22 (ghost wom 3145894 -> real 5755813; real held nonexistent wom 3259035)"),
]

# Tables handled with bespoke unique-key dedup; excluded from the generic reassign.
SPECIAL = {
    ("user_group_association", "player_id"),
    ("web_event_team_members", "player_id"),
    ("web_event_completions", "player_id"),
    ("notification_queue", "player_id"),
}
# player_id columns with NO declared FK (won't block a DELETE, so must be handled explicitly).
NON_FK_COLUMNS = [
    ("player_loot_data", "player_id"),
    ("tracked_task_data", "player_id"),
    ("seasonal_collection", "player_id"),
    ("seasonal_combat_achievement", "player_id"),
    ("seasonal_drops", "player_id"),
    ("seasonal_personal_best", "player_id"),
    ("seasonal_player_pets", "player_id"),
    ("seasonal_quest_completions", "player_id"),
]


def connect():
    return pymysql.connect(
        host="localhost", port=3306, user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"), database="data",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )


def all_reference_columns(cur):
    cur.execute(
        """SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
           WHERE REFERENCED_TABLE_SCHEMA='data' AND REFERENCED_TABLE_NAME='players'
             AND REFERENCED_COLUMN_NAME='player_id'""")
    cols = [(r["TABLE_NAME"], r["COLUMN_NAME"]) for r in cur.fetchall()]
    cols += NON_FK_COLUMNS
    # dedupe preserving order
    seen, out = set(), []
    for tc in cols:
        if tc not in seen:
            seen.add(tc); out.append(tc)
    return out


def count_refs(cur, columns, pid):
    total = {}
    for t, c in columns:
        cur.execute(f"SELECT COUNT(*) n FROM `{t}` WHERE `{c}`=%s", (pid,))
        n = cur.fetchone()["n"]
        if n:
            total[f"{t}.{c}"] = n
    return total


def merge_pair(cur, ghost, real, real_uid, adopt_wom_id, columns, log):
    # Guard: ghost must be a wom_temp stub; real must exist and not be a stub.
    cur.execute("SELECT player_id, player_name, account_hash FROM players WHERE player_id=%s", (ghost,))
    g = cur.fetchone()
    if not g:
        log.append(f"  ghost {ghost} not found — already merged? skipping"); return "skipped"
    if not str(g["account_hash"] or "").startswith("wom_temp_"):
        raise RuntimeError(f"REFUSING: ghost {ghost} account_hash={g['account_hash']!r} is not a wom_temp stub")
    cur.execute("SELECT player_id, player_name, account_hash FROM players WHERE player_id=%s", (real,))
    r = cur.fetchone()
    if not r:
        raise RuntimeError(f"real player {real} not found")
    if str(r["account_hash"] or "").startswith("wom_temp_"):
        raise RuntimeError(f"REFUSING: real target {real} is itself a wom_temp stub")

    log.append(f"  pre-merge ghost refs: {count_refs(cur, columns, ghost)}")

    # 1) user_group_association (uq player_id,user_id,group_id) — dedup groups the real already has, then move + align user_id.
    cur.execute("""DELETE g FROM user_group_association g
                   JOIN user_group_association rr ON rr.group_id=g.group_id AND rr.player_id=%s
                   WHERE g.player_id=%s""", (real, ghost))
    log.append(f"  uga: deleted {cur.rowcount} duplicate group rows the real already had")
    cur.execute("UPDATE user_group_association SET player_id=%s, user_id=%s WHERE player_id=%s",
                (real, real_uid, ghost))
    log.append(f"  uga: moved {cur.rowcount} group rows to real (user_id={real_uid})")
    # Backfill the real's own UGA rows to a consistent user_id (avoid the NULL-race).
    if real_uid is not None:
        cur.execute("UPDATE user_group_association SET user_id=%s WHERE player_id=%s AND (user_id IS NULL OR user_id<>%s)",
                    (real_uid, real, real_uid))
        log.append(f"  uga: backfilled user_id={real_uid} on {cur.rowcount} existing real rows")

    # 2) web_event_team_members (PK team_id,player_id; uq event_id,player_id)
    cur.execute("""DELETE g FROM web_event_team_members g
                   JOIN web_event_team_members rr
                     ON rr.player_id=%s AND (rr.team_id=g.team_id OR rr.event_id=g.event_id)
                   WHERE g.player_id=%s""", (real, ghost))
    log.append(f"  team_members: deleted {cur.rowcount} conflicting rows")
    cur.execute("UPDATE web_event_team_members SET player_id=%s WHERE player_id=%s", (real, ghost))
    log.append(f"  team_members: moved {cur.rowcount} rows")

    # 3) web_event_completions — rewrite the ghost id embedded in the WOM guid so a
    #    later reconciler replay for the real id dedupes instead of double-counting,
    #    then move. uq is (task_id,team_id,submission_guid); real isn't in these teams.
    cur.execute("""UPDATE web_event_completions
                   SET submission_guid=REPLACE(submission_guid, %s, %s)
                   WHERE player_id=%s AND submission_guid LIKE %s""",
                (f":{ghost}:", f":{real}:", ghost, f"wom:%:{ghost}:%"))
    log.append(f"  completions: rewrote guid ghost->real on {cur.rowcount} rows")
    cur.execute("UPDATE web_event_completions SET player_id=%s WHERE player_id=%s", (real, ghost))
    log.append(f"  completions: moved {cur.rowcount} rows")

    # 4) notification_queue (uq notification_type,player_id,group_id,data) — dedup then move.
    cur.execute("""DELETE g FROM notification_queue g
                   JOIN notification_queue rr
                     ON rr.player_id=%s AND rr.notification_type=g.notification_type
                    AND (rr.group_id<=>g.group_id) AND rr.data=g.data
                   WHERE g.player_id=%s""", (real, ghost))
    log.append(f"  notif_queue: deleted {cur.rowcount} duplicate rows")
    cur.execute("UPDATE notification_queue SET player_id=%s WHERE player_id=%s", (real, ghost))
    log.append(f"  notif_queue: moved {cur.rowcount} rows")

    # 5) Everything else: straight reassign. Ghosts have no rows here today, so these
    #    affect 0 rows; a future data-bearing ghost with a per-player unique-key clash
    #    would raise here and abort the transaction (fail loud, not silent orphan).
    moved_other = 0
    for t, c in columns:
        if (t, c) in SPECIAL:
            continue
        cur.execute(f"UPDATE `{t}` SET `{c}`=%s WHERE `{c}`=%s", (real, ghost))
        if cur.rowcount:
            log.append(f"  {t}.{c}: moved {cur.rowcount} rows")
            moved_other += cur.rowcount

    # 6) Pre-DELETE assertion — ghost must have zero remaining references anywhere.
    remaining = count_refs(cur, columns, ghost)
    if remaining:
        raise RuntimeError(f"ABORT: ghost {ghost} still referenced after reassign: {remaining}")
    log.append("  assertion OK: 0 remaining references")

    # 7) Delete the ghost (guarded).
    cur.execute("DELETE FROM players WHERE player_id=%s AND account_hash LIKE 'wom_temp_%%'", (ghost,))
    log.append(f"  deleted ghost player row: {cur.rowcount}")

    # 8) Adopt the roster-authoritative WOM id.
    #    Merging alone does NOT stop the account re-ghosting. fetch_group_members
    #    matches members by players.wom_id first; when the real row's id is one WOM
    #    no longer resolves, that lookup always misses and every sync falls through
    #    to the name-fallback path — the fragile path that minted the ghost. Moving
    #    the real row onto the id the rosters actually report puts it back on the
    #    id-match fast path. players.wom_id is UNIQUE, so this MUST run after the
    #    ghost row (which holds that id) is deleted.
    if adopt_wom_id is not None:
        cur.execute("SELECT wom_id FROM players WHERE player_id=%s", (real,))
        row = cur.fetchone()
        old_wom = row["wom_id"] if row else None
        if old_wom == adopt_wom_id:
            log.append(f"  wom_id: real already on {adopt_wom_id}, no change")
        else:
            cur.execute("SELECT player_id FROM players WHERE wom_id=%s AND player_id<>%s",
                        (adopt_wom_id, real))
            clash = cur.fetchone()
            if clash:
                raise RuntimeError(
                    f"ABORT: wom_id {adopt_wom_id} still held by player {clash['player_id']}")
            cur.execute("UPDATE players SET wom_id=%s WHERE player_id=%s", (adopt_wom_id, real))
            log.append(f"  wom_id: real {real} moved {old_wom} -> {adopt_wom_id}")
    return "merged"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="COMMIT changes (default: dry-run + rollback)")
    ap.add_argument("--only", nargs="*", type=int, help="ghost player_ids to process (default: all)")
    args = ap.parse_args()

    pairs = PAIRS if not args.only else [p for p in PAIRS if p[0] in set(args.only)]
    if not pairs:
        print("No matching pairs."); return

    conn = connect()
    audit = {"when": datetime.now(timezone.utc).isoformat(), "apply": args.apply, "pairs": []}
    try:
        with conn.cursor() as cur:
            columns = all_reference_columns(cur)
            print(f"{len(columns)} player_id reference columns. Mode: {'APPLY' if args.apply else 'DRY-RUN'}\n")
            for ghost, real, real_uid, adopt_wom_id, label in pairs:
                log = [f"PAIR: {label}", f"  ghost={ghost} real={real}"]
                # snapshot for audit/reversibility before mutating
                snap = count_refs(cur, columns, ghost)
                try:
                    result = merge_pair(cur, ghost, real, real_uid, adopt_wom_id, columns, log)
                    if args.apply:
                        conn.commit(); log.append("  COMMITTED")
                    else:
                        conn.rollback(); log.append("  rolled back (dry-run)")
                except Exception as e:
                    conn.rollback()
                    log.append(f"  ERROR (rolled back): {e}")
                    result = "error"
                audit["pairs"].append({"ghost": ghost, "real": real, "label": label,
                                       "pre_merge_refs": snap, "result": result, "log": log})
                print("\n".join(log)); print()
    finally:
        conn.close()

    out = os.path.join(os.path.dirname(__file__), "..", "logs",
                       f"merge_ghost_players_{'apply' if args.apply else 'dryrun'}.json")
    try:
        with open(out, "w") as f:
            json.dump(audit, f, indent=2, default=str)
        print(f"Audit written: {out}")
    except Exception as e:
        print(f"(could not write audit: {e})")


if __name__ == "__main__":
    main()
