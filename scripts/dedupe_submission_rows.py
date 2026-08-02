"""Remove duplicate submission rows that share a ``unique_id`` (GUID).

Why this exists (incident 2026-08-02)
-------------------------------------
The intake API was down 00:51-03:22 UTC and nginx returned 502 to ~423k
submission POSTs. The recovery replayed those submissions — by player clients
and by a manual requeue of 36 dead-lettered entries — and the replays were
written a SECOND time rather than recognised as already-recorded. Nine items
from a single 01:34 kill landed twice, at 06:40 and 10:28 (a 3.8-hour gap).

The cause was in ``data/submissions/common.ensure_can_create``: its duplicate
lookup only looked back one hour, so a replay older than that could not see its
own original. That is fixed in code (the lookup is now unbounded), which stops
NEW duplicates. This script clears the ones already on disk.

What it does, per table: group rows by ``unique_id``, keep exactly one, delete
the rest.

Which row is kept:
  * ``personal_best`` — the row holding the BEST (lowest) ``personal_best``
    time, tie-broken by lowest id. Every row in a group is the same
    (player, npc, team_size) — the app only ever reads one of them — so
    keeping the lowest id blindly could discard a faster time that a later
    update wrote onto the other row. (Group 1772068696-… is exactly this
    shape: id 48196 holds 4000ms, 48197/48198 hold 36000ms.)
  * everything else — the lowest id, i.e. the original write.

Rows with no ``unique_id`` (NULL or empty) are never touched: no dedup key
means nothing to compare. There are ~200k such legacy rows.

Safety:
  * Dry-run by default; ``--apply`` is required to delete anything.
  * Every doomed row is written to ``logs/dedupe_submission_rows_<ts>.json``
    as a complete column-by-column snapshot BEFORE it is deleted, so the
    deletion is reversible by hand.
  * **Refuses to delete a row that anything else still references.** The five
    FKs into these tables (``notified``, ``group_recent_drops``,
    ``drop_group_moderation``, ``video_uploads``, ``drop_splits``) are checked
    first; a referenced row is reported and skipped rather than cascaded, so
    the script can never silently destroy a Discord message record or a split.
    As of 2026-08-02 no duplicate has any dependent.
  * Idempotent: a second run finds nothing and deletes nothing.

Redis: deleting drop rows leaves the monthly leaderboards holding the value the
duplicates contributed, so ``--apply`` rebuilds each affected player with
``force_update_player`` (the same authoritative primitive the player-updates
service uses). ``--skip-force-update`` opts out. The small tables do not feed
the loot leaderboards and need no equivalent.

Scope: the ``drops`` table is 175M rows, so a whole-table GROUP BY is not an
option — it is scanned over a ``date_added`` window (``--since``, default
2026-08-01) which rides ``ix_drops_date_added``. The four small tables are
scanned in full. Run with an earlier ``--since`` to sweep a wider window.

Run (dry-run):
    cd /store/droptracker/disc && venv/bin/python -m scripts.dedupe_submission_rows
Then, once the numbers look right:
    cd /store/droptracker/disc && venv/bin/python -m scripts.dedupe_submission_rows --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import bindparam, create_engine, text  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO_ROOT, "logs")

# Dedicated engine: the shared app engine caps read_timeout at 30s, which is
# too short for the drops window scan (same pattern as the reconcile_* and
# backfill_* scripts).
_maint_engine = create_engine(
    f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@localhost:3306/data",
    pool_size=2,
    max_overflow=2,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10, "read_timeout": 1800, "write_timeout": 300,
                  "charset": "utf8mb4"},
)


class Table:
    """A table to dedupe, and everything that points at it.

    ``dependents`` are (table, column) pairs holding a FK to this table's PK.
    ``keep`` names the strategy for choosing the survivor of a group.
    """

    def __init__(self, name, pk, dependents=(), keep="lowest_id", windowed=False):
        self.name = name
        self.pk = pk
        self.dependents = dependents
        self.keep = keep
        self.windowed = windowed


TABLES = [
    Table("drops", "drop_id", windowed=True, dependents=(
        ("notified", "drop_id"),
        ("group_recent_drops", "drop_id"),
        ("drop_group_moderation", "drop_id"),
        ("video_uploads", "drop_id"),
        ("drop_splits", "drop_id"),
    )),
    Table("personal_best", "id", keep="best_pb", dependents=(("notified", "pb_id"),)),
    Table("collection", "log_id", dependents=(("notified", "clog_id"),)),
    Table("combat_achievement", "id", dependents=(("notified", "ca_id"),)),
    Table("player_pets", "id"),
]


def _fetch_groups(conn, table: Table, since: str):
    """Return {unique_id: [full row dicts]} for every duplicated GUID."""
    window = "AND date_added >= :since" if table.windowed else ""
    params = {"since": since} if table.windowed else {}
    dup_ids = conn.execute(text(f"""
        SELECT unique_id FROM {table.name}
        WHERE unique_id IS NOT NULL AND unique_id <> '' {window}
        GROUP BY unique_id HAVING COUNT(*) > 1
    """), params).scalars().all()
    if not dup_ids:
        return {}
    rows = conn.execute(
        text(f"SELECT * FROM {table.name} WHERE unique_id IN :ids ORDER BY unique_id, {table.pk}")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": dup_ids},
    ).mappings().all()
    groups = {}
    for row in rows:
        groups.setdefault(row["unique_id"], []).append(dict(row))
    # A windowed scan finds the GUID inside the window but the IN-lookup above
    # is unwindowed, so a group can pick up an older sibling. That is correct —
    # the older row is the original and must be the one kept.
    return {guid: rows for guid, rows in groups.items() if len(rows) > 1}


def _pick_keeper(table: Table, rows: list[dict]) -> dict:
    if table.keep == "best_pb":
        return min(rows, key=lambda r: (r["personal_best"], r[table.pk]))
    return min(rows, key=lambda r: r[table.pk])


def _find_dependents(conn, table: Table, ids: list) -> dict:
    """Map doomed pk -> [(dependent_table, column, count)] for anything still
    referencing it. A non-empty result means the row must not be deleted."""
    blocked = {}
    if not ids:
        return blocked
    for dep_table, dep_col in table.dependents:
        rows = conn.execute(
            text(f"SELECT {dep_col} AS ref, COUNT(*) AS n FROM {dep_table} "
                 f"WHERE {dep_col} IN :ids GROUP BY {dep_col}")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).all()
        for ref, n in rows:
            blocked.setdefault(ref, []).append((dep_table, dep_col, n))
    return blocked


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--since", default="2026-08-01",
                    help="date_added floor for the drops scan (default: 2026-08-01). "
                         "The small tables are always scanned in full.")
    ap.add_argument("--skip-force-update", action="store_true",
                    help="skip the Redis rebuild for players whose drops changed")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== dedupe_submission_rows [{mode}] drops window: date_added >= {args.since} ===\n")

    snapshot = {"generated_at": datetime.now().isoformat(), "since": args.since,
                "applied": args.apply, "tables": {}}
    plan = []          # (Table, keeper_pk, [doomed pks])
    blocked_total = 0
    affected_players = set()

    with _maint_engine.connect() as conn:
        for table in TABLES:
            groups = _fetch_groups(conn, table, args.since)
            doomed_rows, keep_count = [], 0
            for guid, rows in sorted(groups.items()):
                keeper = _pick_keeper(table, rows)
                losers = [r for r in rows if r[table.pk] != keeper[table.pk]]
                keep_count += 1
                doomed_rows.extend(losers)

            blocked = _find_dependents(conn, table, [r[table.pk] for r in doomed_rows])
            deletable = [r for r in doomed_rows if r[table.pk] not in blocked]
            blocked_total += len(blocked)

            print(f"{table.name}: {len(groups)} duplicate GUID group(s), "
                  f"{len(doomed_rows)} row(s) to remove, {keep_count} kept"
                  + (f", {len(blocked)} BLOCKED by dependents" if blocked else ""))
            for r in deletable:
                print(f"    - {table.pk}={r[table.pk]} guid={r['unique_id']} "
                      f"player_id={r.get('player_id')} date_added={r.get('date_added')}")
            for pk, refs in blocked.items():
                detail = ", ".join(f"{t}.{c} x{n}" for t, c, n in refs)
                print(f"    ! {table.pk}={pk} SKIPPED — still referenced by {detail}")

            snapshot["tables"][table.name] = {
                "pk": table.pk,
                "keep_rule": table.keep,
                "groups": len(groups),
                "deleted": [{k: _json_safe(v) for k, v in r.items()} for r in deletable],
                "blocked": {str(pk): [[t, c, n] for t, c, n in refs]
                            for pk, refs in blocked.items()},
            }
            if deletable:
                plan.append((table, [r[table.pk] for r in deletable]))
            if table.name == "drops":
                affected_players.update(r["player_id"] for r in deletable if r.get("player_id"))

    total = sum(len(ids) for _, ids in plan)
    print(f"\nTotal rows to delete: {total}"
          + (f"  (blocked: {blocked_total})" if blocked_total else ""))
    if affected_players:
        print(f"Players needing a Redis rebuild: {sorted(affected_players)}")

    if not total:
        print("Nothing to do.")
        return 0

    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_path = os.path.join(LOG_DIR, f"dedupe_submission_rows_{stamp}.json")
    with open(snap_path, "w") as fh:
        json.dump(snapshot, fh, indent=2, default=str)
    print(f"Snapshot written: {snap_path}")

    if not args.apply:
        print("\nDry run — nothing deleted. Re-run with --apply to write.")
        return 0

    deleted = 0
    with _maint_engine.begin() as conn:
        for table, ids in plan:
            res = conn.execute(
                text(f"DELETE FROM {table.name} WHERE {table.pk} IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": ids},
            )
            print(f"  deleted {res.rowcount} from {table.name}")
            deleted += res.rowcount
    print(f"Deleted {deleted} row(s).")

    if affected_players and not args.skip_force_update:
        from db.models.base import get_fresh_session
        from services.redis_updates import force_update_player

        print(f"Rebuilding Redis for {len(affected_players)} player(s)...")
        for pid in sorted(affected_players):
            session = None
            try:
                session = get_fresh_session()
                ok = force_update_player(pid, session)
                print(f"  force_update_player({pid}) -> {ok}")
            except Exception as exc:
                print(f"  force_update_player({pid}) FAILED: {exc}", file=sys.stderr)
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
            time.sleep(0.05)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
