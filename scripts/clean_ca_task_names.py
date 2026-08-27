"""Strip client markup from combat achievement task names already stored.

Why this exists (2026-08-26)
----------------------------
Jagex began wrapping the task name in the ``@ach_comp@`` click-through token, so
the completion message reads "... combat task: @ach_comp@Smite Fight." The
plugin's parser passed the token through and 2,921 rows landed under names like
``@ach_comp@Smite Fight``.

That is not merely ugly. ``ca_processor`` decides "has this player done this
task before?" by matching ``task_name``, so a marked-up name matches nothing: a
task a player had already completed was recorded again and announced again, and
the Discord message showed the raw token.

New submissions are fixed in code — ``utils/ca_tasks.resolve_task_name``, called
from ``data/submissions/ca.py``, cleans and canonicalizes before anything reads
the name. This script repairs the rows written before that landed.

What it does
------------
For every ``combat_achievement`` row whose name contains markup:

* Clean it, and — unless ``--no-verify`` — snap it to the spelling in the
  cache-derived task registry (or the wiki, for tasks released since the
  registry was last rebuilt). An unrecognized name is reported and cleaned but
  never renamed to a guess.
* If cleaning makes the row collide with one the player already has, the row is
  a re-record of a task they had already completed, and is deleted — but only
  when nothing depends on it. Two things can:

  - a row in ``notified``, which is the record of a Discord message that really
    was sent, and must not be left dangling; and
  - a screenshot or video the surviving row does not have, since the re-record
    may well be the only copy of the proof.

  Either way the row is renamed and reported rather than deleted, leaving a
  harmless duplicate instead of a lost reference or a lost screenshot.

``seasonal_combat_achievement`` is deliberately not touched: the live table has
no ``task_name`` column at all — it carries ``ca_id`` — so it holds none of
these names. Note that the ORM model claims otherwise; that drift is a separate
problem.

Safety
------
* Dry-run by default; ``--apply`` is required to write.
* Every row it will change or delete is snapshotted whole to
  ``logs/clean_ca_task_names_<ts>.json`` before anything is written, so the run
  is reversible by hand.
* Idempotent: a second run finds nothing.

Run (dry-run):
    cd /store/droptracker/disc && venv/bin/python -m scripts.clean_ca_task_names
Then:
    cd /store/droptracker/disc && venv/bin/python -m scripts.clean_ca_task_names --apply
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO_ROOT, ".env"))

from sqlalchemy import bindparam, create_engine, text  # noqa: E402

from utils.ca_tasks import (  # noqa: E402
    catalog_index,
    clean_task_name,
    task_key,
    wiki_index,
)

LOG_DIR = os.path.join(REPO_ROOT, "logs")

# Dedicated engine, as in the other maintenance scripts: the shared app engine
# caps read_timeout at 30s.
_maint_engine = create_engine(
    f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@localhost:3306/data",
    pool_size=2,
    max_overflow=2,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10, "read_timeout": 1800, "write_timeout": 300,
                  "charset": "utf8mb4"},
)

TABLE = "combat_achievement"


class _ConnSession:
    """Adapts a SQLAlchemy Connection to the ``.query()`` shape catalog_index wants."""

    def __init__(self, conn):
        self._conn = conn

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        row = self._conn.execute(text(
            "SELECT payload FROM plugin_manifest_sections WHERE `key` = 'combat_achievement_tasks'"
        )).first()
        if row is None:
            return None
        return type("Row", (), {"key": "combat_achievement_tasks", "payload": row[0]})()


#: Cheap prefilter for "might need cleaning", so the 220k-row table is not
#: pulled into memory. It must be a SUPERSET of what ``clean_task_name``
#: actually changes or the backfill and the runtime would disagree forever;
#: the authoritative test is the comparison in :func:`_dirty_rows`. Note that
#: it deliberately does not match a trailing period — four real tasks end in
#: one and cleaning leaves them alone.
_DIRTY_PREFILTER = (
    "task_name LIKE '%@%' OR task_name LIKE '%<%' OR task_name LIKE '% point%)%'"
)


def _dirty_rows(conn):
    """Rows whose stored name is not equal to its cleaned form."""
    rows = conn.execute(text(f"""
        SELECT * FROM {TABLE} WHERE {_DIRTY_PREFILTER} ORDER BY id
    """)).mappings().all()
    return [dict(r) for r in rows if clean_task_name(r["task_name"]) != r["task_name"]]


def _build_index(conn, use_wiki: bool):
    """``task_key`` -> canonical name, catalog first then wiki."""
    index = dict(catalog_index(_ConnSession(conn)))
    print(f"  registry: {len(index)} tasks")
    if use_wiki:
        wiki = asyncio.run(wiki_index(cache=None, refresh=True))
        print(f"  wiki:     {len(wiki)} tasks")
        for key, name in wiki.items():
            index.setdefault(key, name)
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--no-verify", action="store_true",
                    help="strip markup only; do not snap names to the known catalog")
    ap.add_argument("--no-wiki", action="store_true",
                    help="use only the local registry, never api.php")
    args = ap.parse_args()

    with _maint_engine.connect() as conn:
        rows = _dirty_rows(conn)
        print(f"{len(rows)} row(s) carry markup in {TABLE}.task_name")
        if not rows:
            return 0

        index = {} if args.no_verify else _build_index(conn, use_wiki=not args.no_wiki)

        # (player_id, task_name) -> the clean row already holding it, so a
        # rename that collides with a completion the player already has can be
        # recognised. Proof columns come along because a collision is only
        # safe to delete if it is not the only copy of a screenshot.
        existing = {}
        for r in conn.execute(text(
            f"SELECT id, player_id, task_name, image_url, video_url FROM {TABLE} "
            f"WHERE NOT ({_DIRTY_PREFILTER})"
        )).mappings():
            existing.setdefault((r["player_id"], r["task_name"]), dict(r))

        referenced = set(conn.execute(text(
            f"SELECT ca_id FROM notified WHERE ca_id IN "
            f"(SELECT id FROM {TABLE} WHERE {_DIRTY_PREFILTER})"
        )).scalars().all())

        renames, deletes, kept_dupes = [], [], []
        unverified = collections.Counter()

        for row in rows:
            cleaned = clean_task_name(row["task_name"])
            canonical = index.get(task_key(cleaned), cleaned)
            if canonical == cleaned and task_key(cleaned) not in index and not args.no_verify:
                unverified[cleaned] += 1
            entry = {**row, "new_task_name": canonical}
            survivor = existing.get((row["player_id"], canonical))
            if survivor is None:
                renames.append(entry)
                # A placeholder claim, so two dirty rows cleaning to the same
                # name cannot both be renamed onto it.
                existing[(row["player_id"], canonical)] = entry
                continue
            # A re-record of a completion the player already had. Delete it —
            # unless a sent notification points at it (the record of a real
            # Discord message must not dangle) or it is the only copy of a
            # screenshot or video the survivor does not have.
            adds_proof = (
                (row.get("image_url") and not survivor.get("image_url"))
                or (row.get("video_url") and not survivor.get("video_url"))
            )
            if row["id"] in referenced or adds_proof:
                entry["kept_because"] = "notified" if row["id"] in referenced else "proof"
                kept_dupes.append(entry)
            else:
                deletes.append(entry)

        print(f"  rename: {len(renames)}")
        print(f"  delete (re-record of a completion already held, nothing depends on it): {len(deletes)}")
        print(f"  rename but keep as a duplicate (notification or proof depends on it): {len(kept_dupes)}")
        for entry in kept_dupes[:10]:
            print(f"      id={entry['id']} {entry['new_task_name']!r} ({entry['kept_because']})")
        if unverified:
            print(f"  names in neither the registry nor the wiki: {len(unverified)} distinct")
            for name, count in unverified.most_common(20):
                print(f"      {count:6d}  {name!r}")
            print("      (cleaned but not renamed — rebuild the registry if these are real)")

        for entry in renames[:10]:
            print(f"      {entry['task_name']!r} -> {entry['new_task_name']!r}")

        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            return 0

        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot = os.path.join(LOG_DIR, f"clean_ca_task_names_{stamp}.json")
        with open(snapshot, "w") as fh:
            json.dump(
                {"renames": renames, "deletes": deletes, "kept_duplicates": kept_dupes},
                fh, indent=2, default=str,
            )
        print(f"\nSnapshot written to {snapshot}")

    # A fresh connection for the write. The read connection above has already
    # autobegun a transaction on its first execute(), and SQLAlchemy 2.x
    # refuses an explicit begin() on top of that — calling it there raised
    # after the snapshot was written, so the run looked like it had done
    # something when it had changed nothing.
    with _maint_engine.begin() as wconn:
        for entry in renames + kept_dupes:
            wconn.execute(
                text(f"UPDATE {TABLE} SET task_name = :name WHERE id = :id"),
                {"name": entry["new_task_name"], "id": entry["id"]},
            )
        if deletes:
            wconn.execute(
                text(f"DELETE FROM {TABLE} WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": [e["id"] for e in deletes]},
            )
    print(f"Applied: {len(renames) + len(kept_dupes)} renamed, {len(deletes)} deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
