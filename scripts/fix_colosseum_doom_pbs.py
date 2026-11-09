"""Bug #140 data repair: one Colosseum NPC, no impossible times, Doom spelling.

Intake is fixed (utils/npc_names, utils/pb_floors, data/submissions/pb_lines);
this repairs what was written before, in four steps, one transaction:

  1. Impossible times go. A Colosseum "PB" shorter than any whole run (a wave
     split or the Sol fight alone) and a Doom 1 - 8 total that is really a
     single delve level, judged exactly as intake now judges them:
     ``utils.pb_floors.below_floor``, plus, for Doom, a total below the
     player's own eight level bests added up. Zero-time rows on those boards
     go too.
  2. Everything on "Sol Heredit" (12821) moves to "Fortis Colosseum" (13741):
     personal bests (merged where a player has both, fastest record wins, as
     in merge_pb_team_size_brackets), kill counts (highest wins), the hourly
     loot rollup (summed) and the drops themselves. Any boss-record badge held
     on the 12821 slot is retired; the evaluator awards the merged board.
  3. npc_list's "Doom of Mokhaitol (Level: N)" rows are renamed "Mokhaiotl".
  4. Saved names follow: every Hall of Fame boss list and event task naming
     "Sol Heredit" or a "Mokhaitol" level is rewritten (and deduplicated), so
     no group loses a board.

Removed and absorbed rows are written to logs/ before anything is deleted.

Usage
-----
    python -m scripts.fix_colosseum_doom_pbs            # dry run (default)
    python -m scripts.fix_colosseum_doom_pbs --apply    # write changes

Idempotent: a second run finds nothing to do.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from utils.doom_delve import DOOM_NPC_ID
from utils.npc_names import LEGACY_NAME_FIXES, canonical_encounter_name
from utils.pb_floors import DOOM_LEVELS, below_floor, below_level_sum

SOL_ID, COLOSSEUM_ID = 12821, 13741
COLOSSEUM = "Fortis Colosseum"
DOOM_TOTAL = "Doom of Mokhaiotl"
BOSS_LIST_KEY = "personal_best_embed_boss_list"

ROW_COLUMNS = (
    "id, player_id, npc_id, kill_time, personal_best, team_size, new_pb, "
    "image_url, video_url, date_added, used_api, unique_id, precise_timing"
)


def _rows(s, where, params):
    cols = [c.strip() for c in ROW_COLUMNS.split(",")]
    return [dict(zip(cols, r)) for r in s.execute(
        text(f"SELECT {ROW_COLUMNS} FROM personal_best WHERE {where}"), params
    ).fetchall()]


def _delete_pbs(s, ids):
    """Drop PB rows and what hangs off them (both FKs are RESTRICT)."""
    for i in ids:
        s.execute(text("UPDATE notified SET pb_id=NULL WHERE pb_id=:i"), {"i": i})
        s.execute(text("DELETE FROM personal_best_loadouts WHERE pb_id=:i"), {"i": i})
        s.execute(text("DELETE FROM personal_best WHERE id=:i"), {"i": i})


def _record(rows):
    return min(rows, key=lambda r: (
        0 if (r["personal_best"] or 0) > 0 else 1,
        r["personal_best"] or float("inf"),
        r["date_added"] or datetime.max,
        r["id"],
    ))


def _latest(rows):
    return max(rows, key=lambda r: (r["date_added"] or datetime.min, r["id"]))


# --------------------------------------------------------------------------- #
# 1. impossible times
# --------------------------------------------------------------------------- #
def impossible_rows(s):
    junk = []
    for row in _rows(s, "npc_id IN (:a, :b)", {"a": SOL_ID, "b": COLOSSEUM_ID}):
        pb = row["personal_best"] or 0
        if pb <= 0 or below_floor(COLOSSEUM, pb):
            junk.append({**row, "reason": "colosseum below floor"})

    level_bests = {}
    for player_id, npc_id, best in s.execute(text(
        "SELECT player_id, npc_id, personal_best FROM personal_best "
        "WHERE npc_id BETWEEN :lo AND :hi AND personal_best > 0"
    ), {"lo": DOOM_NPC_ID + 1, "hi": DOOM_NPC_ID + DOOM_LEVELS}).fetchall():
        level_bests.setdefault(player_id, []).append(best)
    for row in _rows(s, "npc_id = :n", {"n": DOOM_NPC_ID}):
        pb = row["personal_best"] or 0
        if pb <= 0 or below_floor(DOOM_TOTAL, pb):
            junk.append({**row, "reason": "doom total below floor"})
        elif below_level_sum(pb, level_bests.get(row["player_id"], [])):
            junk.append({**row, "reason": "doom total below own level bests"})
    return junk


# --------------------------------------------------------------------------- #
# 2. Sol Heredit -> Fortis Colosseum
# --------------------------------------------------------------------------- #
def merge_colosseum_pbs(s, apply, skip_ids):
    moved, merged, absorbed = 0, 0, []
    for row in _rows(s, "npc_id = :n", {"n": SOL_ID}):
        if row["id"] in skip_ids:
            continue
        target = _rows(
            s, "npc_id = :n AND player_id = :p AND team_size = :t",
            {"n": COLOSSEUM_ID, "p": row["player_id"], "t": row["team_size"]},
        )
        target = [r for r in target if r["id"] not in skip_ids]
        if not target:
            moved += 1
            if apply:
                s.execute(text("UPDATE personal_best SET npc_id=:c WHERE id=:i"),
                          {"c": COLOSSEUM_ID, "i": row["id"]})
            continue

        merged += 1
        survivor = target[0]
        group = target + [row]
        record, latest = _record(group), _latest(group)
        absorbed.append({**row, "merged_into": survivor["id"]})
        if not apply:
            continue
        s.execute(text("UPDATE notified SET pb_id=:s WHERE pb_id=:l"),
                  {"s": survivor["id"], "l": row["id"]})
        if record["id"] == row["id"]:
            # The loadout table is keyed by pb_id: drop the survivor's first.
            s.execute(text("DELETE FROM personal_best_loadouts WHERE pb_id=:i"), {"i": survivor["id"]})
            s.execute(text("UPDATE personal_best_loadouts SET pb_id=:s WHERE pb_id=:r"),
                      {"s": survivor["id"], "r": row["id"]})
        s.execute(text("DELETE FROM personal_best_loadouts WHERE pb_id=:i"), {"i": row["id"]})
        s.execute(text("DELETE FROM personal_best WHERE id=:i"), {"i": row["id"]})
        s.execute(text(
            "UPDATE personal_best SET personal_best=:pb, precise_timing=:pt, image_url=:img, "
            "video_url=:vid, new_pb=:npb, kill_time=:kt, date_added=:da, used_api=:api WHERE id=:i"
        ), {
            "pb": record["personal_best"], "pt": record["precise_timing"],
            "img": record["image_url"], "vid": record["video_url"], "npb": record["new_pb"],
            "kt": latest["kill_time"], "da": latest["date_added"],
            "api": 1 if any(r["used_api"] for r in group) else 0, "i": survivor["id"],
        })
    return moved, merged, absorbed


def merge_colosseum_other(s, apply):
    """Kill counts, hourly loot rollup and drops. Returns counts."""
    counts = {
        "kc rows": s.execute(text("SELECT COUNT(*) FROM player_npc_kc WHERE npc_id=:n"), {"n": SOL_ID}).scalar(),
        "hourly rows": s.execute(text("SELECT COUNT(*) FROM player_npc_hourly_totals WHERE npc_id=:n"),
                                 {"n": SOL_ID}).scalar(),
    }
    # Drops can't be scanned by npc_id; the rollup names every player who has any.
    players = [p for (p,) in s.execute(text(
        "SELECT DISTINCT player_id FROM player_npc_hourly_totals WHERE npc_id=:n"), {"n": SOL_ID})]
    drop_ids = []
    for p in players:
        drop_ids += [d for (d,) in s.execute(text(
            "SELECT drop_id FROM drops WHERE player_id=:p AND npc_id=:n"), {"p": p, "n": SOL_ID})]
    counts["drops"] = len(drop_ids)
    for table in ("group_recent_drops", "collection", "web_event_effort"):
        n = s.execute(text(f"SELECT COUNT(*) FROM `{table}` WHERE npc_id=:n"), {"n": SOL_ID}).scalar()
        if n:
            raise SystemExit(f"ABORT: {table} has {n} rows on npc {SOL_ID}; this script does not move them")
    if not apply:
        return counts, drop_ids

    # Reading the source through a derived table: MariaDB will not resolve an
    # ON DUPLICATE KEY column when INSERT ... SELECT reads its own table.
    s.execute(text(
        "INSERT INTO player_npc_kc (player_id, npc_id, kill_count, updated_at) "
        "SELECT * FROM (SELECT player_id, :c AS npc_id, kill_count, updated_at "
        "FROM player_npc_kc WHERE npc_id=:s) AS src "
        "ON DUPLICATE KEY UPDATE kill_count=GREATEST(player_npc_kc.kill_count, src.kill_count), "
        "updated_at=GREATEST(player_npc_kc.updated_at, src.updated_at)"
    ), {"c": COLOSSEUM_ID, "s": SOL_ID})
    s.execute(text("DELETE FROM player_npc_kc WHERE npc_id=:s"), {"s": SOL_ID})
    s.execute(text(
        "INSERT INTO player_npc_hourly_totals (player_id, npc_id, date_hour, `partition`, total_value, "
        "drop_count, last_drop_time) "
        "SELECT * FROM (SELECT player_id, :c AS npc_id, date_hour, `partition`, total_value, drop_count, "
        "last_drop_time FROM player_npc_hourly_totals WHERE npc_id=:s) AS src "
        "ON DUPLICATE KEY UPDATE total_value=player_npc_hourly_totals.total_value+src.total_value, "
        "drop_count=player_npc_hourly_totals.drop_count+src.drop_count, "
        "last_drop_time=GREATEST(player_npc_hourly_totals.last_drop_time, src.last_drop_time)"
    ), {"c": COLOSSEUM_ID, "s": SOL_ID})
    s.execute(text("DELETE FROM player_npc_hourly_totals WHERE npc_id=:s"), {"s": SOL_ID})
    for i in range(0, len(drop_ids), 500):
        chunk = drop_ids[i:i + 500]
        params = {f"d{j}": d for j, d in enumerate(chunk)}
        s.execute(text(
            f"UPDATE drops SET npc_id=:c WHERE drop_id IN ({', '.join(':' + k for k in params)})"
        ), {"c": COLOSSEUM_ID, **params})
    return counts, drop_ids


def retire_vanished_badges(s, apply):
    keys = [k for (k,) in s.execute(text(
        "SELECT active_key FROM player_badges WHERE status='active' AND active_key LIKE :k"
    ), {"k": f"npc:{SOL_ID}:%"})]
    if apply and keys:
        s.execute(text(
            "UPDATE player_badges SET status='lost', active_key=NULL, lost_at=NOW() "
            "WHERE status='active' AND active_key LIKE :k"
        ), {"k": f"npc:{SOL_ID}:%"})
    return keys


# --------------------------------------------------------------------------- #
# 3 + 4. names
# --------------------------------------------------------------------------- #
def rename_doom_levels(s, apply):
    renames = []
    for old, new in LEGACY_NAME_FIXES.items():
        row = s.execute(text("SELECT npc_id FROM npc_list WHERE npc_name=:o"), {"o": old}).first()
        if row is None:
            continue
        renames.append((row.npc_id, old, new))
        if apply:
            s.execute(text("UPDATE npc_list SET npc_name=:n WHERE npc_id=:i"), {"n": new, "i": row.npc_id})
    return renames


def _fix_name(name):
    return canonical_encounter_name(name) if name in LEGACY_NAME_FIXES or name == "Sol Heredit" else name


def rewrite_boss_list(raw):
    """The list with folded names replaced and duplicates dropped, in the
    format it was stored in (JSON array or comma list); None if unchanged."""
    raw = (raw or "").strip()
    as_json = None
    if raw.startswith("["):
        try:
            as_json = json.loads(raw)
        except ValueError:
            as_json = None
    names = as_json if isinstance(as_json, list) else [
        p.strip().strip('"').strip("'").strip() for p in raw.replace("[", "").replace("]", "").split(",")
    ]
    out, seen = [], set()
    for name in names:
        name = _fix_name(str(name).strip())
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    new = json.dumps(out, separators=(",", ":")) if isinstance(as_json, list) else ", ".join(out)
    return None if new == raw else new


def rewrite_saved_names(s, apply):
    changed = []
    for row in s.execute(text(
        "SELECT id, group_id, config_value, long_value FROM group_configurations WHERE config_key=:k "
        "AND (config_value LIKE '%Sol Heredit%' OR config_value LIKE '%Mokhaitol%' "
        "OR long_value LIKE '%Sol Heredit%' OR long_value LIKE '%Mokhaitol%')"
    ), {"k": BOSS_LIST_KEY}).fetchall():
        short, long_ = (row.config_value or ""), (row.long_value or "")
        # long_value holds the whole list; config_value is either the same
        # list or a stale, truncated prefix of it.
        source = long_ if len(long_) >= len(short) else short
        new = rewrite_boss_list(source)
        if new is None:
            continue
        changed.append({"id": row.id, "group_id": row.group_id, "config_value": short,
                        "long_value": long_, "new": new})
        if apply:
            # The settings save's own rule (web_api/routes/config.py).
            s.execute(text("UPDATE group_configurations SET config_value=:c, long_value=:l WHERE id=:i"),
                      {"c": new if len(new) <= 255 else "", "l": new, "i": row.id})

    tasks = []
    for task_id, config in s.execute(text(
        "SELECT id, config FROM web_event_tasks WHERE config LIKE '%Mokhaitol%' OR config LIKE '%Sol Heredit%'"
    )).fetchall():
        new = config.replace("Mokhaitol", "Mokhaiotl").replace('"Sol Heredit"', f'"{COLOSSEUM}"')
        if new != config:
            tasks.append({"id": task_id, "old": config, "new": new})
            if apply:
                s.execute(text("UPDATE web_event_tasks SET config=:c WHERE id=:i"), {"c": new, "i": task_id})
    return changed, tasks


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    s = Session()
    try:
        junk = impossible_rows(s)
        by_reason = {}
        for r in junk:
            by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
        print(f"[{mode}] 1. impossible times: {len(junk)} rows {by_reason}")
        for r in junk:
            if r["reason"].startswith("colosseum"):
                print(f"      colosseum id={r['id']} player={r['player_id']} {r['personal_best']}ms")
        if args.apply:
            _delete_pbs(s, [r["id"] for r in junk])
        skip = {r["id"] for r in junk}

        moved, merged, absorbed = merge_colosseum_pbs(s, args.apply, skip)
        counts, drop_ids = merge_colosseum_other(s, args.apply)
        badges = retire_vanished_badges(s, args.apply)
        print(f"[{mode}] 2. Sol Heredit -> Fortis Colosseum: {moved} PBs moved, {merged} merged; "
              f"{counts['kc rows']} kc rows, {counts['hourly rows']} hourly rows, {counts['drops']} drops; "
              f"badges retired: {badges}")

        renames = rename_doom_levels(s, args.apply)
        print(f"[{mode}] 3. npc_list renames: {len(renames)}")
        for npc_id, old, new in renames:
            print(f"      {npc_id}: {old!r} -> {new!r}")

        lists, tasks = rewrite_saved_names(s, args.apply)
        print(f"[{mode}] 4. Hall of Fame lists rewritten: {len(lists)} groups; event tasks: {len(tasks)}")
        for t in tasks:
            print(f"      task {t['id']}: {t['new']}")

        backup = {"junk": junk, "absorbed": absorbed, "moved_drop_ids": drop_ids,
                  "retired_badge_keys": badges, "renames": renames, "boss_lists": lists, "tasks": tasks}
        os.makedirs("logs", exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = f"logs/fix_colosseum_doom_{'apply' if args.apply else 'dryrun'}_{stamp}.json"
        with open(path, "w") as fh:
            json.dump(backup, fh, indent=1, default=str)
        print(f"[{mode}] backup: {path}")

        if args.apply:
            s.commit()
        else:
            s.rollback()
            print("[DRY RUN] nothing was written. Re-run with --apply to commit.")
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()

    if args.apply:
        _clear_caches()


def _clear_caches():
    """Drop the cached boss picker and the touched NPC pages' caches."""
    try:
        from utils.redis import redis_client

        r = redis_client.client
        keys = ["web:pb-boss-names"]
        for npc_id in [SOL_ID, COLOSSEUM_ID, *range(DOOM_NPC_ID, DOOM_NPC_ID + DOOM_LEVELS + 2)]:
            keys += [f"npc:stats:{npc_id}", f"npc:recent:{npc_id}", f"npc:{npc_id}:last_item_drops"]
        print(f"[APPLY] cleared {r.delete(*keys)} cache keys")
    except Exception as e:
        print(f"[APPLY] cache clear failed (they expire on their own): {e}")


if __name__ == "__main__":
    main()
