"""Merge npc_list rows that are variants of the same boss (suggestion #50).

Why this exists
---------------
Intake used to match NPCs by *exact* name, so sources that name one boss
differently minted separate rows and split its data three ways:

  * punctuation/case:  "Tombs of Amascut: Expert Mode" vs "... Expert Mode"
  * the "The " article: "The Whisperer" vs "Whisperer", "The Hueycoatl" vs
    "Hueycoatl" (drops flow under the "The " spelling, PBs under the bare one)
  * outright aliases:  "Crystalline Hunllef" vs "The Gauntlet",
    "Corrupted Hunllef" vs "The/Corrupted Gauntlet"

Intake now normalizes through ``utils.npc_names.npc_match_key`` so no NEW
splits occur; this script repairs the existing rows.

What it does (data-driven, not hardcoded ids)
---------------------------------------------
For every group of npc_list rows sharing a match key with >1 distinct name:

  1. Primary = most drops (direct ``drops`` count — the hourly rollup
     undercounts), then wiki rows, then PBs, then lowest id.
  2. Non-primary rows ("orphans") are merged when they hold player data
     (PBs / drops / clog entries) OR are completely empty shells with no wiki
     table. Catalog game-id variants whose only content is a wiki table
     (Ice troll 649-654 …) are left alone — read-side collapse hides them.
  3. Repointing:
     * ``personal_best`` → primary, then keep only the fastest per
       (player_id, team_size).
     * ``player_npc_hourly_totals`` → sum-merged into the primary's rows
       (unique key player/npc/hour/partition).
     * every other npc_id table (drops, collection, seasonal_drops, …) →
       ``UPDATE IGNORE`` + delete unmovable leftovers (a leftover means the
       primary already has the equivalent unique row).
     * ``xenforo.dt_npc_loot``: adopted by the primary if it has no wiki
       table yet, else deleted (per-spelling duplicate of the same table).
  4. Row deletion: slug/article variants are deleted; ALIAS-named rows
     (the Hunllefs) are kept as empty searchable shells so text search for
     "hunllef" still finds the boss (search maps them to the primary page).
  5. Redis: per-NPC registries of merged ids are dropped; a primary that
     received drops gets its registry dropped too (rebuilds lazily).

Usage
-----
    python -m scripts.merge_duplicate_npcs           # dry run (default)
    python -m scripts.merge_duplicate_npcs --apply   # write changes

Idempotent: a second run finds nothing left to merge. After an --apply that
moved drops, rebuild the per-NPC leaderboards:
    python -m scripts.backfill_npc_leaderboards
"""

import argparse
import sys
from collections import defaultdict

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from utils.npc_names import npc_match_key, npc_slug, strip_the

HOURLY = "player_npc_hourly_totals"


def npc_id_tables(s):
    rows = s.execute(text(
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.COLUMNS "
        "WHERE COLUMN_NAME='npc_id' AND TABLE_SCHEMA IN ('data','xenforo')"
    )).fetchall()
    return [(sch, tbl) for sch, tbl in rows if tbl != "npc_list"]


def refs(s, tables, nid):
    out = {}
    for sch, tbl in tables:
        n = s.execute(text(f"SELECT COUNT(*) FROM `{sch}`.`{tbl}` WHERE npc_id = :nid"),
                      {"nid": nid}).scalar()
        if n:
            out[f"{sch}.{tbl}"] = int(n)
    return out


def stats(s, nid):
    """(drops, wiki_rows, pbs) — drops counted directly, the rollup undercounts."""
    drops = s.execute(text("SELECT COUNT(*) FROM drops WHERE npc_id=:n"), {"n": nid}).scalar()
    wiki = s.execute(text(
        "SELECT COUNT(*) FROM xenforo.dt_npc_loot WHERE npc_id=:n"), {"n": nid}).scalar()
    pbs = s.execute(text(
        "SELECT COUNT(*) FROM personal_best WHERE npc_id=:n"), {"n": nid}).scalar()
    return int(drops or 0), int(wiki or 0), int(pbs or 0)


def dedupe_pbs(s, primary_id, apply_):
    """Keep only the fastest PB per (player_id, team_size) on the primary."""
    pairs = s.execute(text(
        "SELECT player_id, team_size FROM personal_best WHERE npc_id=:n "
        "GROUP BY player_id, team_size HAVING COUNT(*) > 1"
    ), {"n": primary_id}).fetchall()
    removed = 0
    for player_id, team_size in pairs:
        rows = s.execute(text(
            "SELECT id, personal_best, kill_time FROM personal_best "
            "WHERE npc_id=:n AND player_id=:p AND team_size=:t"
        ), {"n": primary_id, "p": player_id, "t": team_size}).fetchall()
        ranked = sorted(rows, key=lambda r: (
            0 if (r[1] or 0) > 0 else 1, r[1] or float("inf"), r[2] or float("inf"), r[0]))
        for row_id, _, _ in ranked[1:]:
            removed += 1
            if apply_:
                s.execute(text("DELETE FROM personal_best WHERE id=:i"), {"i": row_id})
    return len(pairs), removed


def merge_hourly(s, orphan_id, primary_id):
    """Sum-merge the orphan's hourly rollup rows into the primary's."""
    # MariaDB rejects self-referencing INSERT…SELECT…ON DUPLICATE KEY UPDATE
    # (error 1052: every column reference is ambiguous), so merge in two
    # steps: repoint the non-colliding rows wholesale, then fold the
    # colliding ones into the primary's rows via an aliased join-update.
    s.execute(text(f"UPDATE IGNORE {HOURLY} SET npc_id = :p WHERE npc_id = :o"),
              {"p": primary_id, "o": orphan_id})
    s.execute(text(f"""
        UPDATE {HOURLY} dst
        JOIN {HOURLY} src
          ON src.npc_id = :o AND dst.npc_id = :p
         AND dst.player_id = src.player_id
         AND dst.date_hour = src.date_hour
         AND dst.`partition` = src.`partition`
        SET dst.total_value = COALESCE(dst.total_value,0) + COALESCE(src.total_value,0),
            dst.drop_count  = COALESCE(dst.drop_count,0)  + COALESCE(src.drop_count,0),
            dst.last_drop_time = GREATEST(COALESCE(dst.last_drop_time, src.last_drop_time),
                                          COALESCE(src.last_drop_time, dst.last_drop_time))
    """), {"p": primary_id, "o": orphan_id})
    s.execute(text(f"DELETE FROM {HOURLY} WHERE npc_id = :o"), {"o": orphan_id})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    apply_ = args.apply
    mode = "APPLY" if apply_ else "DRY RUN"

    s = Session()
    tables = npc_id_tables(s)
    rows = s.execute(text("SELECT npc_id, npc_name FROM npc_list")).fetchall()
    groups = defaultdict(list)
    for nid, name in rows:
        key = npc_match_key(name)
        if key:
            groups[key].append((int(nid), name))

    merged = deleted = kept_shells = skipped = 0
    cleared_redis_ids = []
    drops_moved_primaries = set()
    for key, members in sorted(groups.items()):
        if len({n for _, n in members}) < 2:
            continue  # multi-id same-spelling rows (Vorkath 8060/8061) are fine
        ranked = sorted(members, key=lambda m: tuple(-x for x in stats(s, m[0])) + (m[0],))
        primary_id, primary_name = ranked[0]
        p_drops, p_wiki, p_pbs = stats(s, primary_id)
        if p_drops == 0 and p_wiki == 0 and p_pbs == 0:
            continue  # all-empty family, nothing to concentrate

        for nid, name in ranked[1:]:
            d, w, p = stats(s, nid)
            has_data = bool(d or p)
            if not has_data and w:
                continue  # catalog game-id variant: wiki table only — leave it
            # Alias-named rows stay as searchable shells; slug/article variants
            # of the primary's own name are deleted outright.
            is_alias_row = strip_the(npc_slug(name)) != strip_the(npc_slug(primary_name))
            r = refs(s, tables, nid)
            action = "keep shell" if is_alias_row else "delete row"
            print(f"[{mode}] merge {nid} {name!r} -> {primary_id} {primary_name!r} "
                  f"({action}; refs={r or '{}'})")

            if apply_:
                # 1. PBs
                if r.get("data.personal_best"):
                    s.execute(text("UPDATE personal_best SET npc_id=:p WHERE npc_id=:o"),
                              {"p": primary_id, "o": nid})
                # 2. hourly rollup (sum-merge, unique-key safe)
                if r.get(f"data.{HOURLY}"):
                    merge_hourly(s, nid, primary_id)
                # 3. wiki table: adopt when the primary lacks one, else drop dupes
                if r.get("xenforo.dt_npc_loot"):
                    if p_wiki == 0:
                        s.execute(text(
                            "UPDATE IGNORE xenforo.dt_npc_loot SET npc_id=:p WHERE npc_id=:o"),
                            {"p": primary_id, "o": nid})
                        p_wiki = stats(s, primary_id)[1]
                    s.execute(text("DELETE FROM xenforo.dt_npc_loot WHERE npc_id=:o"),
                              {"o": nid})
                # 4. everything else generically (drops, collection, seasonal,
                #    caches, config lists): repoint; unique-key leftovers mean
                #    the primary already has the row — drop them.
                for full, _cnt in r.items():
                    sch, tbl = full.split(".", 1)
                    if tbl in ("personal_best", HOURLY, "dt_npc_loot"):
                        continue
                    if tbl == "dt_npc":
                        s.execute(text(f"DELETE FROM `{sch}`.`{tbl}` WHERE npc_id=:o"),
                                  {"o": nid})
                        continue
                    s.execute(text(
                        f"UPDATE IGNORE `{sch}`.`{tbl}` SET npc_id=:p WHERE npc_id=:o"),
                        {"p": primary_id, "o": nid})
                    s.execute(text(f"DELETE FROM `{sch}`.`{tbl}` WHERE npc_id=:o"),
                              {"o": nid})
                if not is_alias_row:
                    s.execute(text("DELETE FROM npc_list WHERE npc_id=:o"), {"o": nid})

            # PB fastest-per-(player, team_size) dedupe / preview
            pb_count = r.get("data.personal_best", 0)
            if pb_count:
                if apply_:
                    pairs, removed = dedupe_pbs(s, primary_id, apply_)
                else:
                    pairs = s.execute(text(
                        "SELECT COUNT(DISTINCT a.player_id, a.team_size) FROM personal_best a "
                        "JOIN personal_best b ON b.player_id=a.player_id "
                        "AND b.team_size=a.team_size AND b.npc_id=:p WHERE a.npc_id=:o"
                    ), {"p": primary_id, "o": nid}).scalar()
                    removed = f"≥{pairs}"
                print(f"        PB dedupe on {primary_id}: {pairs} colliding pairs, "
                      f"{removed} slower rows removed")
            if r.get("data.drops"):
                drops_moved_primaries.add(primary_id)
                print(f"        {r['data.drops']} drops repointed -> {primary_id}")

            merged += 1 if has_data else 0
            if is_alias_row:
                kept_shells += 1
            else:
                deleted += 1
            cleared_redis_ids.append(nid)

    if apply_:
        s.commit()
        cleared_redis_ids.extend(drops_moved_primaries)
        try:
            from utils.redis import redis_client
            conn = getattr(redis_client, "client", None)
            if conn is not None:
                for nid in cleared_redis_ids:
                    conn.delete(f"npc:{nid}:last_item_drops",
                                f"npc:{nid}:last_item_drops:building")
        except Exception as e:
            print(f"(redis cleanup skipped: {e})")
    s.close()
    print(f"\n[{mode}] done: {merged} data merges, {deleted} rows deleted, "
          f"{kept_shells} alias shells kept, {skipped} skipped.")
    if drops_moved_primaries:
        print(f"NOTE: drops moved into npc ids {sorted(drops_moved_primaries)} — "
              f"rebuild per-NPC boards: python -m scripts.backfill_npc_leaderboards")


if __name__ == "__main__":
    main()
