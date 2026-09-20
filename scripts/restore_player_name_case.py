#!/usr/bin/env python3
"""Restore the real in-game spelling of players.player_name (ticket #441).

Background: check_user_by_username returned WOM's ``username`` -- its
standardized key, which is ``.replace(/[-_\s]/g,' ').trim().toLowerCase()`` --
instead of ``displayName``, the account's actual in-game spelling. Every row
first created by submission intake therefore stored an all-lowercase,
separator-folded name ("deadcllck" for DEADCLlCK, "94 fable 92" for 94 Fable 92,
"zuk my feet" for Zuk-My-Feet). Purely cosmetic, but it shows on every board,
embed and profile.

Nothing repaired it afterwards either: all four writers gated their update on
normalize_player_display_equivalence(), which lowercases and folds separators,
so the correct spelling compared equal to the stored one and was dropped -- the
plugin sent "DEADCLlCK" 94 times and we discarded it 94 times.

The code fix (utils/wiseoldman.py + data/submissions/common.py, utils.rsn
better_spelling) covers new rows, and the hourly WOM group sync now heals any
player on a linked clan roster on its next pass. This script is for the rest:
rows whose owner is in no WOM-linked group, where nothing would ever re-supply
the spelling.

Source of truth, in order:
  1. WOM displayName for the row's wom_id (authoritative; --wom, rate-limited).
  2. The spelling the RuneLite plugin submitted, from notification_queue.
     DropTrackerPlugin.getLocalPlayerName() does no normalization, so this is
     the game's own spelling.

A candidate is only ever accepted when it is the SAME NAME -- identical once
'-', '_' and whitespace are folded and case is ignored. A genuine rename can
never be applied by this script.

Safe by construction: players.player_name has no UNIQUE key (only the plain
index ix_players_player_name), the column collation utf8mb4_general_ci is
case-insensitive, and the generated player_name_norm column lowercases and
folds separators -- so a case/separator-only rewrite leaves every name-keyed
lookup and index seek returning exactly what it returned before.

SAFETY:
  * Dry-run by default -- everything runs in one transaction that is ROLLED
    BACK, printing the exact before/after. Pass --apply to COMMIT.
  * --wom spends the shared WOM budget (100 req/65s); it is throttled to
    1 req/sec and is OFF by default.

Usage:
  python scripts/restore_player_name_case.py                    # dry-run, submitted names
  python scripts/restore_player_name_case.py --unrostered-only  # dry-run, skip rows the hourly sync heals
  python scripts/restore_player_name_case.py --wom              # dry-run, ask WOM for truth
  python scripts/restore_player_name_case.py --apply --unrostered-only   # APPLY
"""
import argparse
import os
import re
import sys
import time
import unicodedata

import pymysql
import urllib.request
import json as _json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

WOM_API = "https://api.wiseoldman.net/v2/players/id/{}"
WOM_SLEEP = 1.0


def fold(name: str) -> str:
    """The one equivalence rule: separators folded, case ignored (utils.rsn)."""
    if name is None:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = s.replace("-", " ").replace("_", " ")
    return " ".join(s.split()).lower()


def wom_display_name(wom_id):
    try:
        req = urllib.request.Request(
            WOM_API.format(wom_id), headers={"User-Agent": "DropTracker-name-restore"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return (_json.load(r) or {}).get("displayName") or None
    except Exception as e:
        print(f"    ! WOM lookup failed for {wom_id}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="COMMIT (default: dry-run)")
    ap.add_argument("--wom", action="store_true",
                    help="ask WOM for displayName instead of using submitted names")
    ap.add_argument("--unrostered-only", action="store_true",
                    help="skip players on a WOM-linked roster (the hourly sync heals those)")
    ap.add_argument("--limit", type=int, default=0, help="cap rows processed (0 = no cap)")
    args = ap.parse_args()

    conn = pymysql.connect(
        host="127.0.0.1", user=os.getenv("DB_USER"), password=os.getenv("DB_PASS"),
        database="data", charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    cur = conn.cursor()

    roster_clause = """
      AND NOT EXISTS (SELECT 1 FROM user_group_association uga
                      JOIN groups g ON g.group_id = uga.group_id
                      WHERE uga.player_id = p.player_id
                        AND g.wom_id IS NOT NULL AND g.wom_id > 0)
    """ if args.unrostered_only else ""

    # Fold notification_queue down to one submitted spelling per name ONCE.
    # As a correlated subquery this re-scans the queue per player and never
    # finishes; as a temp table + join it is seconds.
    cur.execute("DROP TEMPORARY TABLE IF EXISTS sub_names")
    cur.execute("""
        CREATE TEMPORARY TABLE sub_names (
            folded VARCHAR(32) NOT NULL PRIMARY KEY,
            submitted VARCHAR(32) NOT NULL
        )
    """)
    cur.execute("""
        INSERT IGNORE INTO sub_names (folded, submitted)
        SELECT LOWER(REPLACE(REPLACE(n, '_', ' '), '-', ' ')) AS folded, n
          FROM (SELECT JSON_UNQUOTE(JSON_EXTRACT(data, '$.player_name')) AS n
                  FROM notification_queue
                 WHERE JSON_EXTRACT(data, '$.player_name') IS NOT NULL
                 GROUP BY 1) q
         WHERE n IS NOT NULL AND n <> ''
    """)

    cur.execute(f"""
        SELECT p.player_id, p.player_name, p.wom_id, s.submitted
          FROM players p
          LEFT JOIN sub_names s
                 ON s.folded = LOWER(REPLACE(REPLACE(p.player_name, '_', ' '), '-', ' '))
         WHERE p.account_hash NOT LIKE 'wom_temp_%%'
           AND p.player_name IS NOT NULL
           {roster_clause}
         ORDER BY p.player_id
    """)
    rows = cur.fetchall()

    changes, skipped_rename, no_source, skipped_lossy = [], 0, 0, 0
    for r in rows:
        stored = r["player_name"] or ""
        candidate = None
        if args.wom and r["wom_id"]:
            candidate = wom_display_name(r["wom_id"])
            time.sleep(WOM_SLEEP)
        if not candidate:
            candidate = r["submitted"]
        if not candidate:
            no_source += 1
            continue
        candidate = str(candidate).strip()
        if candidate == stored:
            continue
        if fold(candidate) != fold(stored):
            # Not a spelling difference -- a real rename. Never touched here.
            skipped_rename += 1
            continue
        if not any(c.isupper() for c in candidate) and any(c.isupper() for c in stored):
            # Never destroy capitalisation we already hold. WOM echoes its
            # standardized key as displayName when it has no display spelling
            # (id 2082247 -> "r8d" while the game name is "R8d"), and a stale
            # submitted row can be folded too. Mirrors utils.rsn.better_spelling.
            skipped_lossy += 1
            continue
        changes.append((r["player_id"], stored, candidate))
        if args.limit and len(changes) >= args.limit:
            break

    print(f"scanned {len(rows)} rows; {len(changes)} need a spelling restore "
          f"({skipped_rename} genuine renames skipped, {skipped_lossy} refused as "
          f"case-destroying, {no_source} with no source)")
    for pid, old, new in changes[:40]:
        print(f"  {pid:>8}  {old!r}  ->  {new!r}")
    if len(changes) > 40:
        print(f"  ... and {len(changes) - 40} more")

    if not changes:
        conn.close()
        return 0

    for pid, _old, new in changes:
        cur.execute("UPDATE players SET player_name = %s WHERE player_id = %s", (new, pid))

    if args.apply:
        conn.commit()
        print(f"\nAPPLIED: {len(changes)} rows committed.")
    else:
        conn.rollback()
        print(f"\nDRY RUN: rolled back. Re-run with --apply to commit {len(changes)} rows.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
