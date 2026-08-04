"""Seed / inspect the split-source allowlist (utils/split_policy).

Resolves the curated encounter list in ``scripts/split_eligible_sources.json``
to concrete ``npc_id`` values against ``npc_list`` and stores them in the
global-group config row that the intake path reads.

Dry-run by default (repo idiom); ``--apply`` writes. Writing the list does NOT
turn enforcement on: the policy mode is separate and ships as ``shadow``, so a
seeded list only starts changing outcomes once someone runs ``--set-mode
enforce`` on purpose.

  # see what would be seeded, and which names don't resolve
  ./venv/bin/python -m scripts.seed_split_policy

  # write the allowlist (still shadow mode - no behaviour change)
  ./venv/bin/python -m scripts.seed_split_policy --apply

  # what has the gate seen? (shadow impact: real splits kept vs stopped)
  ./venv/bin/python -m scripts.seed_split_policy --impact

  # flip enforcement on (or back off) once the list is agreed
  ./venv/bin/python -m scripts.seed_split_policy --set-mode enforce
  ./venv/bin/python -m scripts.seed_split_policy --set-mode shadow
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, text  # noqa: E402

from db.models.base import session  # noqa: E402
from utils import split_policy  # noqa: E402

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "split_eligible_sources.json")


def load_seed() -> dict:
    with open(SEED_PATH) as fh:
        return json.load(fh)


def resolve_names(names) -> dict:
    """``{lowered_name: [npc_id, ...]}`` for exact (case-insensitive) matches."""
    clean = [str(n).strip().lower() for n in names if str(n).strip()]
    if not clean:
        return {}
    rows = session.execute(
        text("SELECT npc_id, npc_name FROM npc_list WHERE LOWER(npc_name) IN :n").bindparams(
            bindparam("n", expanding=True)
        ),
        {"n": clean},
    ).fetchall()
    out = defaultdict(list)
    for npc_id, npc_name in rows:
        out[str(npc_name).strip().lower()].append(int(npc_id))
    return dict(out)


def build(seed) -> tuple:
    """(mapping {npc_id: category}, per-encounter rows, unresolved names)."""
    mapping = {}
    rows = []
    unresolved = []
    for entry in seed.get("entries", []):
        names = entry.get("names", [])
        resolved = resolve_names(names)
        ids = set(int(i) for i in entry.get("npc_ids", []))
        for name in names:
            found = resolved.get(str(name).strip().lower())
            if not found:
                unresolved.append((entry.get("encounter", "?"), name))
                continue
            ids.update(found)
        category = entry.get("category", split_policy.CATEGORY_TEAM_BOSS)
        for npc_id in ids:
            mapping[npc_id] = category
        rows.append({
            "encounter": entry.get("encounter", "?"),
            "category": category,
            "status": entry.get("status", "proposed"),
            "npc_ids": sorted(ids),
            "note": entry.get("note", ""),
        })
    return mapping, rows, unresolved


def print_plan(rows, mapping, unresolved, mode):
    print(f"Split-source allowlist plan — {len(mapping)} npc_id(s) "
          f"across {len(rows)} encounters")
    print(f"Current policy mode: {mode}"
          f"{'  (gate is INERT — nothing is blocked)' if mode != split_policy.MODE_ENFORCE else '  (ENFORCING)'}\n")
    for status in ("confirmed", "proposed", "question"):
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        print(f"--- {status.upper()} ({len(group)} encounters) ---")
        for r in group:
            print(f"  {r['encounter']:<32} {r['category']:<10} {len(r['npc_ids']):>3} ids")
            if r["note"]:
                print(f"      {r['note']}")
        print()
    if unresolved:
        print(f"!! {len(unresolved)} name(s) did not resolve against npc_list "
              f"(they contribute nothing):")
        for encounter, name in unresolved:
            print(f"   {encounter}: {name!r}")
        print()


def print_impact():
    """Shadow-mode reality check: real split events kept vs stopped."""
    snap = split_policy.impact_snapshot()
    blocked, allowed = snap["blocked"], snap["allowed"]
    total_blocked = sum(v["count"] for v in blocked.values())
    total_allowed = sum(allowed.values())
    print("Split-policy impact since counters were last cleared")
    print(f"  splits that would be KEPT    : {total_allowed}")
    print(f"  splits that would be STOPPED : {total_blocked}\n")
    if not blocked:
        print("  Nothing has been counted yet. Counters only move when a split "
              "actually would have run (participants resolved + group has "
              "split_gp_tracking on) AND the allowlist has been seeded.")
        return
    names = split_policy.resolve_names(session, list(blocked))
    print(f"  {'npc_id':>7}  {'source':<38} {'blocked splits':>14}")
    for npc_id, info in sorted(blocked.items(), key=lambda kv: -kv[1]["count"]):
        label = info["name"] or names.get(npc_id, f"npc #{npc_id}")
        print(f"  {npc_id:>7}  {label[:38]:<38} {info['count']:>14}")
    print("\n  Anything here that SHOULD be splittable belongs in the allowlist "
          "before enforcement goes on.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the allowlist (default: dry run)")
    ap.add_argument("--impact", action="store_true",
                    help="show what the gate has counted, then exit")
    ap.add_argument("--clear-impact", action="store_true",
                    help="reset the shadow counters")
    ap.add_argument("--set-mode", choices=split_policy.VALID_MODES,
                    help="set the policy mode (off | shadow | enforce)")
    ap.add_argument("--show", action="store_true",
                    help="print the allowlist currently stored, then exit")
    args = ap.parse_args()

    if args.clear_impact:
        split_policy.clear_impact()
        print("Shadow counters cleared.")
        return

    if args.impact:
        print_impact()
        return

    if args.set_mode:
        previous = split_policy.get_mode(session)
        split_policy.set_mode(session, args.set_mode)
        print(f"Policy mode: {previous} -> {args.set_mode}")
        if args.set_mode == split_policy.MODE_ENFORCE:
            stored = split_policy.get_eligible(session)
            print(f"ENFORCING against {len(stored)} allowlisted npc_id(s). "
                  f"Splits at any other source will now be skipped.")
            if not stored:
                print("WARNING: the allowlist is EMPTY, so the gate stays "
                      "permissive by design. Seed it with --apply first.")
        print("Every service picks this up within "
              f"{int(split_policy.POLICY_TTL)}s (no restart needed).")
        return

    if args.show:
        stored = split_policy.get_eligible(session)
        names = split_policy.resolve_names(session, list(stored))
        print(f"Stored allowlist: {len(stored)} npc_id(s), "
              f"mode={split_policy.get_mode(session)}")
        by_cat = defaultdict(list)
        for npc_id, category in stored.items():
            by_cat[category].append(npc_id)
        for category, ids in sorted(by_cat.items()):
            print(f"\n  [{category}] {len(ids)} ids")
            for npc_id in sorted(ids):
                print(f"    {npc_id:>7}  {names.get(npc_id, '?')}")
        return

    seed = load_seed()
    mapping, rows, unresolved = build(seed)
    mode = split_policy.get_mode(session)
    print_plan(rows, mapping, unresolved, mode)

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to store the list.")
        return

    split_policy.set_eligible(session, mapping)
    print(f"APPLIED — {len(mapping)} npc_id(s) stored.")
    print(f"Policy mode is still {split_policy.get_mode(session)!r}: no split "
          f"behaviour has changed. Use --impact after a day to see what "
          f"enforcement would do, then --set-mode enforce.")


if __name__ == "__main__":
    main()
