"""Fold truncated PB team-size labels into the game's own brackets (suggestion #153).

Some activities do not store a personal best per exact head count — the game
buckets them and puts only the bucket's label on the completion line ("Team
size: 16-23 players"). The plugin's chat parser matched ``\\d+``, which takes the
leading digits and leaves the rest, so the same Chambers raid was recorded as
team size "16" by the plugin and "16-23" by the clan-chat and adventure-log
paths, which keep the label whole. ``team_size`` is part of a PB row's identity,
so that produced two boards for one raid: neither could ever beat the other, the
Hall of Fame rendered both, and a genuine improvement on one was invisible on
the other.

The plugin regex is fixed and intake now folds through
``utils.npc_names.canonical_team_size``. This script repairs the rows written
before that, in the style of ``scripts/cap_pb_team_sizes.py``:

  1. Relabel every foldable row to its bracket ("16" -> "16-23").
  2. Where a player then holds several rows on one board — the usual case, since
     the split is exactly what the bug produced — merge them into one:

       * the surviving row keeps the fastest ``personal_best`` of the group,
         along with that record's screenshot, video and ``new_pb`` marker;
       * ``kill_time`` and ``date_added`` come from the group's most recent row,
         so the board still reflects the last kill recorded on it;
       * ``used_api`` is true if any merged row was API-sourced;
       * the record's stored loadout follows it onto the survivor.

     Merged-away rows are written to ``logs/`` as JSON before deletion. Both
     tables that reference ``personal_best.id`` do so with RESTRICT, so
     ``notified.pb_id`` is repointed at the survivor and the loadout rows are
     moved or dropped first.

Usage
-----
    python -m scripts.merge_pb_team_size_brackets            # dry run (default)
    python -m scripts.merge_pb_team_size_brackets --apply    # write changes

Idempotent: bracket labels fold to themselves, so a second run is a no-op. Only
``personal_best`` is touched; the seasonal mirror has no team_size column.
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from utils.npc_names import (
    bracket_team_size,
    canonical_team_size,
    clamp_team_size,
    sanitize_team_size,
)

ROW_COLUMNS = (
    "id, player_id, npc_id, kill_time, personal_best, team_size, new_pb, "
    "image_url, video_url, date_added, used_api, unique_id"
)


def _as_dict(row):
    return dict(zip([c.strip() for c in ROW_COLUMNS.split(",")], row))


def _repair_class(npc_name, raw) -> str:
    """Why this label is wrong: the boss's party ceiling, or its bracketing.

    Two different bugs put mislabelled rows in this table, and they are worth
    applying separately: "cap" rows are the contaminated-roster raids of
    suggestion #140 (a 9-man Theatre of Blood), "bracket" rows are the
    truncated bucket labels of suggestion #153 (a "16" Chambers raid).
    """
    if clamp_team_size(npc_name, raw) != sanitize_team_size(raw):
        return "cap"
    return "bracket"


def _record_row(rows):
    """The row holding the board's best time — a zero/NULL best is never one."""
    return min(
        rows,
        key=lambda r: (
            0 if (r["personal_best"] or 0) > 0 else 1,
            r["personal_best"] or float("inf"),
            r["date_added"] or datetime.max,
            r["id"],
        ),
    )


def _latest_row(rows):
    return max(rows, key=lambda r: (r["date_added"] or datetime.min, r["id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument(
        "--only",
        choices=("bracket", "cap", "all"),
        default="all",
        help="repair only truncated bucket labels (bracket, suggestion #153), "
        "only over-cap raid teams (cap, suggestion #140), or both (default)",
    )
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    s = Session()
    npc_names = {
        nid: name
        for nid, name in s.execute(text("SELECT npc_id, npc_name FROM npc_list")).fetchall()
    }

    def target_label(npc_name, raw):
        """The label intake would write today, restricted to the selected class."""
        if args.only == "all":
            return canonical_team_size(npc_name, raw)
        if args.only == "cap":
            return clamp_team_size(npc_name, raw)
        return bracket_team_size(npc_name, sanitize_team_size(raw))

    # Which (npc, label) pairs move at all — cheap, and it scopes the row scan.
    foldable_pairs = {}
    for npc_id, team_size in s.execute(
        text("SELECT DISTINCT npc_id, team_size FROM personal_best")
    ).fetchall():
        target = target_label(npc_names.get(npc_id), team_size)
        if target != team_size:
            foldable_pairs[(npc_id, team_size)] = target

    if not foldable_pairs:
        print(f"[{mode}] no personal_best rows carry a mislabelled team size ({args.only}).")
        s.close()
        return

    print(f"[{mode}] mislabelled (boss, label) pairs [--only {args.only}]:")
    for (npc_id, raw), target in sorted(foldable_pairs.items(), key=lambda kv: (npc_names.get(kv[0][0], ""), kv[0][1])):
        count = s.execute(
            text("SELECT COUNT(*) FROM personal_best WHERE npc_id=:n AND team_size=:ts"),
            {"n": npc_id, "ts": raw},
        ).scalar()
        cls = _repair_class(npc_names.get(npc_id), raw)
        print(f"    [{cls:7}] {npc_names.get(npc_id, npc_id)}: {raw!r} -> {target!r}  ({count} rows)")

    # Every (player, npc) that owns at least one foldable row. The whole
    # player+npc row set is then pulled, because the row this one merges into
    # already carries the correct label and would not match a foldable filter.
    affected = set()
    for npc_id, raw in foldable_pairs:
        for (player_id,) in s.execute(
            text("SELECT DISTINCT player_id FROM personal_best WHERE npc_id=:n AND team_size=:ts"),
            {"n": npc_id, "ts": raw},
        ).fetchall():
            affected.add((player_id, npc_id))

    relabelled = 0
    merged_groups = 0
    deleted = []
    for player_id, npc_id in sorted(affected):
        npc_name = npc_names.get(npc_id)
        rows = [
            _as_dict(r)
            for r in s.execute(
                text(f"SELECT {ROW_COLUMNS} FROM personal_best WHERE player_id=:p AND npc_id=:n"),
                {"p": player_id, "n": npc_id},
            ).fetchall()
        ]
        # Group by the label each row ends up with. Bucketing in Python rather
        # than filtering on team_size keeps the dry run honest: nothing has been
        # rewritten yet, so the rows about to collide still hold their old
        # labels and a WHERE team_size=:target would not find them.
        boards = {}
        for row in rows:
            boards.setdefault(target_label(npc_name, row["team_size"]), []).append(row)

        for target, group in boards.items():
            movers = [r for r in group if r["team_size"] != target]
            if not movers:
                continue

            if len(group) == 1:
                relabelled += 1
                if args.apply:
                    s.execute(
                        text("UPDATE personal_best SET team_size=:ts WHERE id=:i"),
                        {"ts": target, "i": group[0]["id"]},
                    )
                continue

            merged_groups += 1
            settled = [r for r in group if r["team_size"] == target]
            survivor = min(settled or movers, key=lambda r: r["id"])
            record = _record_row(group)
            latest = _latest_row(group)
            losers = [r for r in group if r["id"] != survivor["id"]]
            deleted.extend(
                {**r, "date_added": r["date_added"].isoformat() if r["date_added"] else None,
                 "merged_into": survivor["id"], "npc_name": npc_name, "target_team_size": target}
                for r in losers
            )

            if not args.apply:
                continue

            for loser in losers:
                s.execute(
                    text("UPDATE notified SET pb_id=:sid WHERE pb_id=:lid"),
                    {"sid": survivor["id"], "lid": loser["id"]},
                )
            # pb_id is the loadout table's primary key, so the record's loadout
            # can only move onto the survivor once the survivor's own is gone.
            if record["id"] != survivor["id"]:
                s.execute(
                    text("DELETE FROM personal_best_loadouts WHERE pb_id=:i"),
                    {"i": survivor["id"]},
                )
                s.execute(
                    text("UPDATE personal_best_loadouts SET pb_id=:sid WHERE pb_id=:rid"),
                    {"sid": survivor["id"], "rid": record["id"]},
                )
            for loser in losers:
                s.execute(
                    text("DELETE FROM personal_best_loadouts WHERE pb_id=:i"), {"i": loser["id"]}
                )
                s.execute(text("DELETE FROM personal_best WHERE id=:i"), {"i": loser["id"]})

            s.execute(
                text(
                    "UPDATE personal_best SET team_size=:ts, personal_best=:pb, kill_time=:kt, "
                    "date_added=:da, image_url=:img, video_url=:vid, new_pb=:npb, used_api=:api "
                    "WHERE id=:i"
                ),
                {
                    "ts": target,
                    "pb": record["personal_best"],
                    "kt": latest["kill_time"],
                    "da": latest["date_added"],
                    "img": record["image_url"],
                    "vid": record["video_url"],
                    "npb": record["new_pb"],
                    "api": 1 if any(r["used_api"] for r in group) else 0,
                    "i": survivor["id"],
                },
            )

    # A held boss-record badge is keyed "npc:<id>:<team_size>", and
    # services.badges.evaluate_boss_records only walks slots that still exist in
    # personal_best. Once a label is gone the evaluator never visits it again,
    # so a badge left on it would advertise a board nobody can see or beat
    # forever. Retire those holders the same way a transfer does — the evaluator
    # awards the merged board's slot on its next run.
    retired = 0
    for npc_id, raw in sorted(foldable_pairs):
        active_key = f"npc:{npc_id}:{raw}"
        held = s.execute(
            text("SELECT COUNT(*) FROM player_badges WHERE active_key=:k AND status='active'"),
            {"k": active_key},
        ).scalar()
        if not held:
            continue
        retired += held
        print(f"[{mode}] retiring {held} held badge(s) on vanished slot {active_key!r}")
        if args.apply:
            s.execute(
                text(
                    "UPDATE player_badges SET status='lost', active_key=NULL, lost_at=NOW() "
                    "WHERE active_key=:k AND status='active'"
                ),
                {"k": active_key},
            )

    backup_path = None
    if deleted:
        os.makedirs("logs", exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"logs/pb_bracket_merge_{'apply' if args.apply else 'dryrun'}_{stamp}.json"
        with open(backup_path, "w") as fh:
            json.dump(deleted, fh, indent=2, default=str)

    if args.apply:
        s.commit()
    s.close()

    print(
        f"\n[{mode}] {relabelled} rows relabelled with no collision; "
        f"{merged_groups} boards merged, absorbing {len(deleted)} duplicate rows; "
        f"{retired} held badges retired."
    )
    if backup_path:
        print(f"[{mode}] merged-away rows written to {backup_path}")
    if not args.apply:
        print("[DRY RUN] nothing was written. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
