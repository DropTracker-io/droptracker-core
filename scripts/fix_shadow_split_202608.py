#!/usr/bin/env python3
"""
One-off remediation for the 2026-08-02 Tumeken's shadow split (group 296,
"Unhinged Campfire").

What happened
-------------
A 4-way ToA: Expert Mode shadow split was recorded by hand across three
submissions, because neither the website's submit form nor the Discord
``/submit`` command can express a split:

  * drop 180509282 — ``stuffmyvoid``   got the shadow logged a second time
    (777,800,000) as a stand-in for their share;
  * drop 180516134 — ``puzzled life``  logged 17x Blood shard (126,124,615)
    as a stand-in for their quarter;
  * drop 180517207 — ``WI Beer Guy``   holds the real drop, but with no split
    participants, so the full 777,800,000 sat on his group score.

The fourth participant is not tracked by DropTracker at all, which is why a
plain 3-way split would over-credit everyone: the denominator has to be 4.

What this script does
---------------------
1. HIDE the two stand-in drops (``drops.hidden = 1``), mirroring the admin
   hide path in ``services/entry_modifier.py::_handle_hide_toggle``: delete the
   drop's ``player_points`` rows, mark the ``notified`` row ``hidden``, and
   decrement the ``player_{item,npc}_hourly_totals`` rollups (those aggregate
   at intake and do NOT filter hidden drops, so a hide alone leaves the site's
   item/boss composition and the monthly recap overstated).
2. SPLIT drop 180517207 four ways at 194,450,000 each by inserting ``drop_splits``
   rows for the two tracked participants. The untracked fourth player is counted
   in the denominator but credited to nobody, so 194,450,000 of the shadow is
   deliberately credited to no one.
3. Repair Redis: force-rebuild the two participants (which re-applies their new
   split credits from ``drop_splits``) and apply the receiver's downward
   adjustment for ``WI Beer Guy``.

The drop row's own value is left at the full 777,800,000 — DropTracker models a
split as a GROUP-leaderboard redistribution only, so the global leaderboard and
the receiver's personal loot total are untouched. (Confirmed with the owner
2026-08-02.)

Dry-run by default; pass ``--apply`` to write. Idempotent: re-running after a
successful apply reports "already done" and touches nothing. Before writing it
dumps every affected row to a JSON backup next to the script.

    ./venv/bin/python -m scripts.fix_shadow_split_202608
    ./venv/bin/python -m scripts.fix_shadow_split_202608 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db import Session, Drop, Player, PlayerPoints  # noqa: E402
from db.models.drop_split import DropSplit  # noqa: E402

# ── The facts this remediation is pinned to ──────────────────────────────────
GROUP_ID = 296          # "Unhinged Campfire" — the group with split_gp_tracking=1
PARTITION = 202608
SPLIT_WAYS = 4          # 3 tracked participants + 1 untracked

RECEIVER = {"player_id": 5755880, "name": "WI Beer Guy"}
PARTICIPANTS = [
    {"player_id": 5758194, "name": "stuffmyvoid"},
    {"player_id": 5033, "name": "puzzled life"},
]

SPLIT_DROP_ID = 180517207          # the real shadow, to be split 4 ways

# Stand-in submissions to hide. Each carries the rollup rows its value landed in.
HIDE = [
    {
        "drop_id": 180509282,
        "player_id": 5758194,
        "who": "stuffmyvoid",
        "what": "Tumeken's shadow (uncharged) x1",
        "value": 777_800_000,
        "item_hourly": {"player_id": 5758194, "item_id": 27277, "date_hour": "2026-08-02-21"},
        "npc_hourly": {"player_id": 5758194, "npc_id": 13970, "date_hour": "2026-08-02-21"},
    },
    {
        "drop_id": 180516134,
        "player_id": 5033,
        "who": "puzzled life",
        "what": "Blood shard x17",
        "value": 126_124_615,
        "item_hourly": {"player_id": 5033, "item_id": 24777, "date_hour": "2026-08-02-22"},
        "npc_hourly": {"player_id": 5033, "npc_id": 9756, "date_hour": "2026-08-02-22"},
    },
]


def _fmt(n) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def _backup(session, path: str) -> None:
    """Dump every row this script may touch, so an apply is recoverable."""
    drop_ids = [h["drop_id"] for h in HIDE] + [SPLIT_DROP_ID]
    ids = ",".join(str(d) for d in drop_ids)
    payload = {"taken_at": datetime.now().isoformat(), "drop_ids": drop_ids}
    for label, sql in (
        ("drops", f"SELECT * FROM drops WHERE drop_id IN ({ids})"),
        ("player_points", f"SELECT * FROM player_points WHERE entry_id IN ({ids})"),
        ("notified", f"SELECT * FROM notified WHERE drop_id IN ({ids})"),
        ("drop_splits", f"SELECT * FROM drop_splits WHERE drop_id IN ({ids})"),
        (
            "player_item_hourly_totals",
            "SELECT * FROM player_item_hourly_totals WHERE "
            "(player_id=5758194 AND item_id=27277 AND date_hour='2026-08-02-21') OR "
            "(player_id=5033 AND item_id=24777 AND date_hour='2026-08-02-22')",
        ),
        (
            "player_npc_hourly_totals",
            "SELECT * FROM player_npc_hourly_totals WHERE "
            "(player_id=5758194 AND npc_id=13970 AND date_hour='2026-08-02-21') OR "
            "(player_id=5033 AND npc_id=9756 AND date_hour='2026-08-02-22')",
        ),
    ):
        result = session.execute(text(sql))
        cols = list(result.keys())
        payload[label] = [
            {c: (v.isoformat() if isinstance(v, datetime) else v) for c, v in zip(cols, row)}
            for row in result.fetchall()
        ]
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"  backup written -> {path}")


def _hourly_row(session, table: str, key: dict):
    where = " AND ".join(f"{k}=:{k}" for k in key)
    return session.execute(
        text(f"SELECT id, total_value, drop_count FROM {table} WHERE {where}"), key
    ).fetchone()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()
    apply = args.apply
    mode = "APPLY" if apply else "DRY RUN"

    session = Session()
    drop = session.query(Drop).filter(Drop.drop_id == SPLIT_DROP_ID).first()
    if drop is None:
        print(f"FATAL: drop {SPLIT_DROP_ID} not found — aborting.")
        return 1

    drop_value = int(drop.value) * int(drop.quantity)
    split_value = drop_value // SPLIT_WAYS
    receiver_adjustment = split_value - drop_value

    print(f"=== Tumeken's shadow split remediation — {mode} ===\n")
    print(f"Group {GROUP_ID} (split_gp_tracking=1), partition {PARTITION}\n")

    # ── 1. The two stand-in drops ────────────────────────────────────────────
    print("1. HIDE the stand-in submissions")
    to_hide = []
    for h in HIDE:
        d = session.query(Drop).filter(Drop.drop_id == h["drop_id"]).first()
        if d is None:
            print(f"   drop {h['drop_id']}: MISSING — skipping")
            continue
        if d.hidden:
            print(f"   drop {h['drop_id']} ({h['who']}, {h['what']}): already hidden — skip")
            continue
        pts = session.query(PlayerPoints).filter(PlayerPoints.entry_id == h["drop_id"]).all()
        item_row = _hourly_row(session, "player_item_hourly_totals", h["item_hourly"])
        npc_row = _hourly_row(session, "player_npc_hourly_totals", h["npc_hourly"])
        to_hide.append((h, d, pts, item_row, npc_row))
        print(f"   drop {h['drop_id']} ({h['who']}, {h['what']}) value {_fmt(h['value'])}")
        print(f"      hidden       0 -> 1")
        print(f"      player_points delete {len(pts)} row(s): "
              f"{[(p.id, p.group_id, p.amount) for p in pts]}")
        print(f"      notified      status -> 'hidden'")
        if item_row:
            print(f"      item_hourly  id={item_row[0]} total_value "
                  f"{_fmt(item_row[1])} -> {_fmt(item_row[1] - h['value'])}, "
                  f"drop_count {item_row[2]} -> {item_row[2] - 1}")
        if npc_row:
            print(f"      npc_hourly   id={npc_row[0]} total_value "
                  f"{_fmt(npc_row[1])} -> {_fmt(npc_row[1] - h['value'])}, "
                  f"drop_count {npc_row[2]} -> {npc_row[2] - 1}")

    # ── 2. The 4-way split ───────────────────────────────────────────────────
    print(f"\n2. SPLIT drop {SPLIT_DROP_ID} ({RECEIVER['name']}) {SPLIT_WAYS} ways")
    print(f"   drop value {_fmt(drop_value)} // {SPLIT_WAYS} = {_fmt(split_value)} each")
    existing = {
        r.player_id: r
        for r in session.query(DropSplit).filter(
            DropSplit.drop_id == SPLIT_DROP_ID, DropSplit.group_id == GROUP_ID
        ).all()
    }
    new_participants = []
    for p in PARTICIPANTS:
        if p["player_id"] in existing:
            row = existing[p["player_id"]]
            print(f"   {p['name']:<14} drop_splits row already exists "
                  f"(id={row.id}, {_fmt(row.split_value)}) — skip")
            continue
        new_participants.append(p)
        print(f"   {p['name']:<14} + drop_splits row, group score +{_fmt(split_value)}")
    print(f"   {'<untracked>':<14} counted in the denominator, credited to nobody "
          f"({_fmt(split_value)} goes to no one)")
    if new_participants:
        print(f"   {RECEIVER['name']:<14} group score {_fmt(drop_value)} -> "
              f"{_fmt(split_value)}  (adjustment {_fmt(receiver_adjustment)})")
    else:
        print(f"   {RECEIVER['name']:<14} receiver adjustment already applied — skip")

    if not to_hide and not new_participants:
        print("\nNothing to do — already remediated.")
        return 0

    if not apply:
        print(f"\n(dry run — no changes written. Re-run with --apply.)")
        return 0

    # ── APPLY ────────────────────────────────────────────────────────────────
    backup_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"fix_shadow_split_202608_backup_{datetime.now():%Y%m%dT%H%M%S}.json",
    )
    print("\nApplying...")
    _backup(session, backup_path)

    for h, d, pts, item_row, npc_row in to_hide:
        d.hidden = True
        session.query(PlayerPoints).filter(
            PlayerPoints.entry_id == h["drop_id"]
        ).delete(synchronize_session="fetch")
        session.execute(
            text("UPDATE notified SET status='hidden', date_updated=NOW() WHERE drop_id=:d"),
            {"d": h["drop_id"]},
        )
        if item_row:
            session.execute(
                text("UPDATE player_item_hourly_totals SET total_value=total_value-:v, "
                     "drop_count=drop_count-1, quantity=GREATEST(quantity-:q, 0) WHERE id=:i"),
                {"v": h["value"], "q": int(d.quantity), "i": item_row[0]},
            )
        if npc_row:
            session.execute(
                text("UPDATE player_npc_hourly_totals SET total_value=total_value-:v, "
                     "drop_count=drop_count-1 WHERE id=:i"),
                {"v": h["value"], "i": npc_row[0]},
            )
        print(f"  hid drop {h['drop_id']} ({h['who']})")

    for p in new_participants:
        session.add(DropSplit(
            drop_id=SPLIT_DROP_ID,
            player_id=p["player_id"],
            group_id=GROUP_ID,
            split_value=split_value,
        ))
        print(f"  + drop_splits row for {p['name']} ({_fmt(split_value)})")

    session.commit()
    print("  DB committed.")

    # ── Redis repair ─────────────────────────────────────────────────────────
    # Rebuild the two participants from the DB: this drops the hidden stand-in
    # drops AND re-applies their brand-new drop_splits credits in one pass
    # (_force_update_player_internal -> _apply_split_credits).
    from services.redis_updates import loot_tracker

    for p in PARTICIPANTS:
        ok = loot_tracker.force_update_player(p["player_id"], session_to_use=session)
        print(f"  redis rebuild {p['name']}: {'ok' if ok else 'FAILED'}")

    # The receiver's downward adjustment is NOT reconstructible by a rebuild —
    # _apply_split_credits only replays rows where the player was a PARTICIPANT,
    # so a force-rebuild of the receiver would silently restore the full value.
    # Apply it incrementally here, exactly once (guarded by new_participants).
    if new_participants:
        loot_tracker.add_split_credit(
            RECEIVER["player_id"], receiver_adjustment, PARTITION, GROUP_ID
        )
        print(f"  redis {RECEIVER['name']}: group {GROUP_ID} adjusted by "
              f"{_fmt(receiver_adjustment)}")

    print(f"\nDone. Backup: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
