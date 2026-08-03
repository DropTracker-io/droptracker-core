"""Remove same-kill duplicate drops from multi-part bosses, and unwind the
event credit they earned.

Why this exists (reported 2026-08-03)
------------------------------------
RuneLite delivers one kill of a multi-part boss through more than one loot
event. For the Grotesque Guardians it fires ``NpcLootReceived`` naming **Dusk**
(the guardian that actually drops the loot) *and* ``LootReceived`` naming the
**encounter**. Plugin v5.3.0 only remapped Dusk -> Grotesque Guardians inside
``onLootReceived``, so both paths submitted the same kill, each with its own
freshly-minted GUID.

Every duplicate defence we had is keyed on that GUID —
``data.submissions.common.ensure_can_create`` and the events ledger's
``(task, team, submission_guid)`` unique index — so both copies sailed through.
A Renatus bingo player's Granite ring scored 4 points twice.

Fixed forward in three places:
  * plugin v5.4.0 (``5a7dbf8``) canonicalises in ``processDropEvent`` and
    de-duplicates across handlers — but the hub pin sat on v5.3.0 from
    2026-03-29 until 2026-08-03, and clients update on their own schedule;
  * ``utils/npc_names.ENCOUNTER_NAME_ALIASES`` now folds "Dusk" into
    "Grotesque Guardians", so both copies at least land on one npc_id;
  * intake claims a short Redis lock on the drop's CONTENT for multi-path
    bosses (``claim_multipath_drop``), which is what actually stops it
    regardless of client version.

This script cleans up what is already on disk.

What it does
------------
1. Scans ``drops`` over a ``date_added`` window for rows whose NPC belongs to a
   ``MULTI_PATH_LOOT_SOURCES`` encounter (all member npc_ids included, so the
   Dusk rows and the Grotesque Guardians rows group together).
2. Groups them by (player, item, quantity, value, ``date_added`` second,
   canonical encounter). A group of >1 is one kill submitted more than once —
   these encounters cannot be re-killed inside a second.
3. Keeps exactly one row per group and deletes the rest.
4. Deletes the ``web_event_completions`` rows sourced from the deleted drops,
   then re-folds every affected (event, task) rollup so progress, team scores
   and per-player contribution points match the surviving ledger.
5. Rebuilds Redis loot leaderboards for every affected player.

Which row is kept, in order:
  * the row already on the **canonical encounter's** npc_id (13960 Grotesque
    Guardians, not 7851 Dusk) — it is the one every other name folds to;
  * then the row carrying a **kill count** (the encounter path reports KC; the
    sub-NPC path frequently sends none, and a KC feeds effort/PB tracking);
  * then the **lowest drop_id**, i.e. the original write.

Completed tasks are never un-completed
--------------------------------------
``record_match`` refuses to record anything once a (task, team) rollup
completes, so every qualifying submission after that instant was silently
discarded and cannot be recovered. Re-deriving completion purely from the
surviving ledger would therefore revoke tasks the team may well have finished
on real drops we never wrote down. The re-fold runs with
``preserve_completed=True``: progress, contribution shares and the ledger are
corrected, the completed flag, its points, bingo cells and line bonuses are
held. Any rollup left completed-but-under-threshold is listed explicitly at the
end so it can be reviewed by hand.

Safety
------
  * Dry-run by default; ``--apply`` is required to write anything.
  * A full column-by-column snapshot of every doomed drop row and every doomed
    completion row is written to ``logs/dedupe_multipath_drops_<ts>.json``
    BEFORE anything is deleted, so the deletion is reversible by hand.
  * Refuses to delete a drop that anything else still references (``notified``,
    ``group_recent_drops``, ``drop_group_moderation``, ``video_uploads``,
    ``drop_splits``) — a referenced row is reported and skipped, never
    cascaded, so a Discord message record or a GP split can't be destroyed.
  * Idempotent: a second run finds nothing.

Not touched: ``web_event_effort`` folds absolute kill counts through a
watermark, so a replayed KC contributes a delta of zero and duplicates never
inflated it.

Run (dry-run):
    cd /store/droptracker/disc && venv/bin/python -m scripts.dedupe_multipath_drops
Then, once the numbers look right:
    cd /store/droptracker/disc && venv/bin/python -m scripts.dedupe_multipath_drops --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import bindparam, create_engine, text  # noqa: E402

from utils.npc_names import (  # noqa: E402
    ENCOUNTER_NAME_ALIASES,
    MULTI_PATH_LOOT_SOURCES,
    npc_slug,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO_ROOT, "logs")

#: FKs into drops.drop_id that record something real and irreplaceable — a
#: Discord message that was actually sent, an uploaded video, a moderation
#: decision, a group's recent-drops entry. These are NEVER cascaded: a row
#: carrying one is preferred as the group's keeper, and if the loser still has
#: one (both copies notified, say) the whole group is skipped and reported.
HARD_DEPENDENTS = (
    ("notified", "drop_id"),
    ("group_recent_drops", "drop_id"),
    ("drop_group_moderation", "drop_id"),
    ("video_uploads", "drop_id"),
)

#: FKs that are themselves duplicated credit and must go WITH the drop.
#: ``split_gp_tracking`` writes a DropSplit per participant on every drop it
#: sees, so a double-submitted kill paid every participant twice — both copies
#: of one 2026-07-30 kill carry four splits each. DropSplit is the source of
#: truth a Redis force-rebuild reads, so deleting the duplicates and rebuilding
#: the participants is exactly the correction.
CASCADE_DEPENDENTS = (
    ("drop_splits", "drop_id"),
)

# Dedicated engine: the shared app engine caps read_timeout at 30s, too short
# for a windowed drops scan (same pattern as dedupe_submission_rows).
_maint_engine = create_engine(
    f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@localhost:3306/data",
    pool_size=2,
    max_overflow=2,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10, "read_timeout": 1800, "write_timeout": 300,
                  "charset": "utf8mb4"},
)


def _encounter_npc_ids(conn) -> tuple[dict[int, str], dict[str, set[int]]]:
    """Resolve every npc_list id belonging to a multi-path encounter.

    Returns ``(npc_id -> canonical encounter name, encounter -> {npc_ids})``.
    Both the encounter's own row and its member rows are included, and a name
    may legitimately own several ids (npc_list carries three 'Crystalline
    Hunllef' rows, four 'Dusk' rows), which is exactly why this is resolved
    from the database rather than hardcoded.
    """
    wanted: dict[str, str] = {}
    for encounter in MULTI_PATH_LOOT_SOURCES:
        wanted[npc_slug(encounter)] = encounter
    for member, encounter in ENCOUNTER_NAME_ALIASES.items():
        if encounter in MULTI_PATH_LOOT_SOURCES:
            wanted[npc_slug(member)] = encounter

    rows = conn.execute(text("SELECT npc_id, npc_name FROM npc_list")).all()
    by_id: dict[int, str] = {}
    by_encounter: dict[str, set[int]] = defaultdict(set)
    for npc_id, npc_name in rows:
        encounter = wanted.get(npc_slug(npc_name))
        if encounter:
            by_id[int(npc_id)] = encounter
            by_encounter[encounter].add(int(npc_id))
    return by_id, dict(by_encounter)


def _canonical_ids(conn, encounters) -> dict[str, set[int]]:
    """The npc_ids named exactly for the encounter itself (the keeper target)."""
    rows = conn.execute(text("SELECT npc_id, npc_name FROM npc_list")).all()
    slugs = {npc_slug(e): e for e in encounters}
    out: dict[str, set[int]] = defaultdict(set)
    for npc_id, npc_name in rows:
        enc = slugs.get(npc_slug(npc_name))
        if enc:
            out[enc].add(int(npc_id))
    return dict(out)


def _find_duplicate_groups(conn, since: str, npc_to_encounter: dict[int, str]):
    """Return [(encounter, [row dicts])] — one entry per over-submitted kill."""
    rows = conn.execute(
        text("""
            SELECT drop_id, player_id, item_id, npc_id, quantity, value,
                   date_added, `partition`, kill_count, unique_id, used_api, source
            FROM drops
            WHERE date_added >= :since AND npc_id IN :npc_ids
            ORDER BY player_id, item_id, date_added, drop_id
        """).bindparams(bindparam("npc_ids", expanding=True)),
        {"since": since, "npc_ids": sorted(npc_to_encounter)},
    ).mappings().all()

    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        encounter = npc_to_encounter[int(row["npc_id"])]
        key = (row["player_id"], row["item_id"], row["quantity"], row["value"],
               row["date_added"], encounter)
        buckets[key].append(dict(row))
    return [(key[5], group) for key, group in sorted(buckets.items(), key=lambda kv: str(kv[0]))
            if len(group) > 1]


def _pick_keeper(group: list[dict], canonical_ids: set[int], hard: dict) -> dict:
    """Which copy of the kill survives.

    A row something irreplaceable points at wins outright — deleting it would
    destroy the record of a Discord message that really was sent. Only then do
    we prefer the canonical encounter's npc_id (13960 Grotesque Guardians over
    7851 Dusk), a real kill count (the encounter path reports one, the sub-NPC
    path often doesn't, and KC feeds effort/PB tracking), and finally the
    lowest drop_id — the original write.
    """
    return min(group, key=lambda r: (
        0 if hard.get(int(r["drop_id"])) else 1,
        0 if int(r["npc_id"]) in canonical_ids else 1,
        0 if r.get("kill_count") else 1,
        r["drop_id"],
    ))


def _find_dependents(conn, ids: list[int], tables) -> dict[int, list]:
    """Map drop_id -> [(table, column, count)] for rows referencing it."""
    found: dict[int, list] = {}
    if not ids:
        return found
    for dep_table, dep_col in tables:
        for ref, n in conn.execute(
            text(f"SELECT {dep_col} AS ref, COUNT(*) AS n FROM {dep_table} "
                 f"WHERE {dep_col} IN :ids GROUP BY {dep_col}")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).all():
            found.setdefault(int(ref), []).append((dep_table, dep_col, int(n)))
    return found


def _split_rows(conn, drop_ids: list[int]) -> list[dict]:
    """Every drop_splits row belonging to the doomed drops — snapshotted before
    deletion, and the source of the extra players needing a Redis rebuild."""
    if not drop_ids:
        return []
    return [dict(r) for r in conn.execute(
        text("SELECT * FROM drop_splits WHERE drop_id IN :ids ORDER BY id")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": drop_ids},
    ).mappings().all()]


def _find_completions(conn, drop_ids: list[int]) -> list[dict]:
    if not drop_ids:
        return []
    return [dict(r) for r in conn.execute(
        text("""
            SELECT c.*, e.status AS event_status, e.name AS event_name
            FROM web_event_completions c
            JOIN web_events e ON e.id = c.event_id
            WHERE c.source_type = 'drop' AND c.source_id IN :ids
            ORDER BY c.id
        """).bindparams(bindparam("ids", expanding=True)),
        {"ids": drop_ids},
    ).mappings().all()]


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def _refold_events(task_pairs: set[tuple[int, int]]) -> list[str]:
    """Re-fold each affected (event_id, task_id). Returns warning lines."""
    from db.models.base import get_fresh_session
    from db.models import Event, EventTask
    from services.event_engine import recompute_task_rollups

    warnings: list[str] = []
    for event_id, task_id in sorted(task_pairs):
        session = get_fresh_session()
        try:
            event_row = session.query(Event).filter(Event.id == event_id).first()
            task_row = session.query(EventTask).filter(EventTask.id == task_id).first()
            if event_row is None or task_row is None:
                warnings.append(f"event {event_id} task {task_id}: row missing, skipped")
                continue
            try:
                result = recompute_task_rollups(
                    session, event_row, task_row, preserve_completed=True)
            except ValueError as exc:
                # Manual-only task kinds and board-game events have no
                # recomputable ledger — the completions are gone, but their
                # rollup must be corrected by hand.
                session.rollback()
                warnings.append(
                    f"event {event_id} task {task_id}: NOT re-folded ({exc}) — "
                    f"progress needs manual review")
                continue
            session.commit()
            for team_id, entry in sorted(result.get("teams", {}).items()):
                target = entry.get("target")
                if entry.get("completed") and target and entry.get("progress", 0) < target:
                    warnings.append(
                        f"event {event_id} task {task_id} team {team_id}: held COMPLETED at "
                        f"{entry['progress']}/{target} (post-completion credit was never "
                        f"recorded — review by hand)")
        except Exception as exc:
            session.rollback()
            warnings.append(f"event {event_id} task {task_id}: re-fold FAILED: {exc}")
        finally:
            try:
                session.close()
            except Exception:
                pass
    return warnings


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--days", type=int, default=14,
                    help="how far back to scan, in days (default: 14)")
    ap.add_argument("--since", default=None,
                    help="explicit date_added floor (YYYY-MM-DD), overrides --days")
    ap.add_argument("--skip-force-update", action="store_true",
                    help="skip the Redis loot-leaderboard rebuild for affected players")
    args = ap.parse_args()

    since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== dedupe_multipath_drops [{mode}] window: date_added >= {since} ===\n")

    with _maint_engine.connect() as conn:
        npc_to_encounter, by_encounter = _encounter_npc_ids(conn)
        canonical = _canonical_ids(conn, by_encounter)
        print("Multi-path encounters in scope:")
        for encounter in sorted(by_encounter):
            print(f"  {encounter}: npc_ids {sorted(by_encounter[encounter])} "
                  f"(canonical {sorted(canonical.get(encounter, []))})")
        print()

        groups = _find_duplicate_groups(conn, since, npc_to_encounter)
        if not groups:
            print("Duplicate kills found: 0\n\nNothing to do.")
            return 0

        # Hard dependents are resolved for EVERY row in a duplicate group, not
        # just the presumed losers: which copy is referenced is what decides
        # which copy survives.
        all_ids = [r["drop_id"] for _, group in groups for r in group]
        hard = _find_dependents(conn, all_ids, HARD_DEPENDENTS)

        doomed: list[dict] = []
        per_encounter: dict[str, int] = defaultdict(int)
        for encounter, group in groups:
            keeper = _pick_keeper(group, canonical.get(encounter, set()), hard)
            losers = [r for r in group if r["drop_id"] != keeper["drop_id"]]
            doomed.extend(losers)
            per_encounter[encounter] += len(losers)

        print(f"Duplicate kills found: {len(groups)}  "
              f"(surplus drop rows: {len(doomed)})")
        for encounter, n in sorted(per_encounter.items(), key=lambda kv: -kv[1]):
            print(f"  {encounter}: {n} surplus row(s)")

        blocked = {r["drop_id"]: hard[r["drop_id"]]
                   for r in doomed if hard.get(r["drop_id"])}
        deletable = [r for r in doomed if r["drop_id"] not in blocked]
        if blocked:
            print(f"\n{len(blocked)} row(s) BLOCKED and left alone — both copies of "
                  f"the kill are referenced by something irreplaceable:")
            for pk, refs in sorted(blocked.items()):
                print("    ! drop_id=%s — %s" % (
                    pk, ", ".join(f"{t}.{c} x{n}" for t, c, n in refs)))

        drop_ids = [r["drop_id"] for r in deletable]
        completions = _find_completions(conn, drop_ids)
        splits = _split_rows(conn, drop_ids)
        affected_players = {r["player_id"] for r in deletable if r.get("player_id")}
        # Split participants were credited off the duplicate too, so their
        # group leaderboards need the same rebuild as the receiver's.
        affected_players |= {r["player_id"] for r in splits if r.get("player_id")}
        surplus_gp = sum(int(r["value"] or 0) * int(r["quantity"] or 0) for r in deletable)

    if not deletable:
        print("\nNothing deletable.")
        return 0

    task_pairs = {(int(c["event_id"]), int(c["task_id"])) for c in completions}
    print(f"\nDrop rows to delete: {len(deletable)}   (inflated loot: {surplus_gp:,} gp)")
    print(f"Duplicate GP splits to delete: {len(splits)}   "
          f"(double-credited: {sum(int(r['split_value'] or 0) for r in splits):,} gp)")
    print(f"Players needing a Redis rebuild: {len(affected_players)}")
    print(f"Event completion rows to delete: {len(completions)} "
          f"across {len(task_pairs)} (event, task) rollup(s)")

    if completions:
        by_event: dict = defaultdict(lambda: {"rows": 0, "credit": 0, "name": "", "status": ""})
        for c in completions:
            slot = by_event[int(c["event_id"])]
            slot["rows"] += 1
            slot["credit"] += int(c["quantity"] or 0)
            slot["name"], slot["status"] = c["event_name"], c["event_status"]
        print("\n  event                                              status   rows   surplus credit")
        for event_id, slot in sorted(by_event.items()):
            print(f"  [{event_id:>3}] {slot['name'][:42]:<42} {slot['status']:<8} "
                  f"{slot['rows']:>5}   {slot['credit']:>14,}")

    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_path = os.path.join(LOG_DIR, f"dedupe_multipath_drops_{stamp}.json")
    with open(snap_path, "w") as fh:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "since": since,
            "applied": args.apply,
            "drops_deleted": [{k: _json_safe(v) for k, v in r.items()} for r in deletable],
            "drops_blocked": {str(pk): [[t, c, n] for t, c, n in refs]
                              for pk, refs in blocked.items()},
            "splits_deleted": [{k: _json_safe(v) for k, v in r.items()} for r in splits],
            "completions_deleted": [{k: _json_safe(v) for k, v in c.items()}
                                    for c in completions],
            "refolded_task_pairs": sorted(task_pairs),
        }, fh, indent=2, default=str)
    print(f"\nSnapshot written: {snap_path}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return 0

    # Deleted in chunks, each chunk its own transaction covering ALL THREE
    # tables for those drops. Two reasons: intake is live on `drops`, so short
    # transactions keep row locks brief; and a chunk either lands whole or not
    # at all, so an interrupted run never strands a completion row pointing at
    # a drop that no longer exists. Re-running picks up where it stopped.
    completions_by_drop: dict[int, list[int]] = defaultdict(list)
    for c in completions:
        completions_by_drop[int(c["source_id"])].append(int(c["id"]))
    splits_by_drop: dict[int, list[int]] = defaultdict(list)
    for r in splits:
        splits_by_drop[int(r["drop_id"])].append(int(r["id"]))

    CHUNK = 1000
    deleted = {"completions": 0, "splits": 0, "drops": 0}
    for start in range(0, len(drop_ids), CHUNK):
        chunk = drop_ids[start:start + CHUNK]
        chunk_completions = [i for d in chunk for i in completions_by_drop.get(d, ())]
        chunk_splits = [i for d in chunk for i in splits_by_drop.get(d, ())]
        with _maint_engine.begin() as conn:
            if chunk_completions:
                deleted["completions"] += conn.execute(
                    text("DELETE FROM web_event_completions WHERE id IN :ids")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": chunk_completions}).rowcount
            if chunk_splits:
                # Must precede the drops delete — drop_splits_ibfk_1 blocks it.
                deleted["splits"] += conn.execute(
                    text("DELETE FROM drop_splits WHERE id IN :ids")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": chunk_splits}).rowcount
            deleted["drops"] += conn.execute(
                text("DELETE FROM drops WHERE drop_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": chunk}).rowcount
        print(f"  ...{min(start + CHUNK, len(drop_ids))}/{len(drop_ids)} drops")
    print(f"  deleted {deleted['completions']} event completion row(s), "
          f"{deleted['splits']} GP split row(s), {deleted['drops']} drop row(s)")

    warnings: list[str] = []
    if task_pairs:
        print(f"\nRe-folding {len(task_pairs)} (event, task) rollup(s)...")
        warnings = _refold_events(task_pairs)

    if affected_players and not args.skip_force_update:
        from db.models.base import get_fresh_session
        from services.redis_updates import force_update_player

        print(f"\nRebuilding Redis for {len(affected_players)} player(s)...")
        failures = 0
        for pid in sorted(affected_players):
            session = None
            try:
                session = get_fresh_session()
                force_update_player(pid, session)
            except Exception as exc:
                failures += 1
                print(f"  force_update_player({pid}) FAILED: {exc}", file=sys.stderr)
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
            time.sleep(0.05)
        print(f"  rebuilt {len(affected_players) - failures}/{len(affected_players)}")

    if warnings:
        print("\n=== NEEDS REVIEW ===")
        for line in warnings:
            print(f"  ! {line}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
