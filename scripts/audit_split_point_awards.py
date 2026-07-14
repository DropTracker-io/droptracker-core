#!/usr/bin/env python3
"""
Audit (and, with explicit opt-in, remediate) group point-split rows that were
over-credited by the historical ``equal_split`` ceil-division bug.

Background
----------
Fixed 2026-07-14 in ``data/submissions/point_awards.py``: when a group has
``point_sharing`` enabled with method ``equal_split``/``equal``, the engine used
to divide a drop's point award (``point_award``) among the receiver + N in-group
targets with **ceil** division and hand *every* participant that same ceil share:

    per_person = ceil(point_award / (N + 1))       # each target AND the receiver

That over-distributes: a 10-point drop split among receiver+2 gave 4 to all three
(12 distributed for a 10-point item). The fix floor-divides and gives the
indivisible remainder to the *receiver* only (see ``_compute_split_shares``):

    per_target = floor(point_award / (N + 1))
    receiver   = per_target + (point_award mod (N + 1))

Net effect on historical rows (equal_split only):
  * each **target** row was over-credited by ``ceil - floor`` = 1 point when the
    award did not divide evenly (0 when it did);
  * the **receiver** row got ``ceil`` where it should have ``floor + remainder``,
    so the receiver was *under*-credited by ``remainder - 1`` when remainder >= 2.

``award_all`` and disabled sharing are unaffected (unchanged by the fix).

What this script does
---------------------
Identifies each split event from the target rows' reason strings
(``"<base> (received by <X> from <Y...>)"``), reconstructs the pre-split
``point_award`` from the *same group config the engine used* (drop award/divisor,
per-item/npc mods, stacks flag, min/max bounds) using the engine's own pure
helpers, and compares the recorded amounts to both:
  * the buggy ceil distribution (used to VALIDATE the reconstruction), and
  * the correct floor distribution (the target state).

Only events whose recorded amounts exactly reproduce the buggy ceil distribution
are classified ``confirmed`` (safe to correct). Everything else is ``uncertain``
(award_all groups, deleted groups with purged drops, capped-by-max amounts,
manual edits, timed-event multipliers, ...) and is reported but never corrected.

Dry-run by default: prints counts + per-player/per-group deltas and (optionally)
writes a CSV. It NEVER writes to the DB unless ``--apply`` is passed together
with ``--i-understand-this-writes`` (the correction path is intentionally gated).
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Session, PlayerPoints, Drop, GroupPointConfig, GroupPointMods, GroupConfiguration, Player  # noqa: E402
# Reuse the engine's own pure arithmetic so the audit matches production exactly.
from data.submissions.point_awards import (  # noqa: E402
    _round_half_up_div,
    _floor_div,
    _compute_split_shares,
    _apply_submission_point_bounds,
)

SPLIT_MARKER = " (received by "
# Default drop config (mirrors _create_default_config in point_awards.py).
DEFAULT_DROP_AWARD = 1
DEFAULT_DROP_DIVISOR = 10_000_000
EQUAL_METHODS = ("equal_split", "equal")


def _ceil_div(value: int, divisor: int) -> int:
    """Historical (buggy) ceil division used for the split. Removed from the
    engine on 2026-07-14; kept here to reconstruct the pre-fix behaviour."""
    if divisor <= 0 or value <= 0:
        return 0
    return (value + divisor - 1) // divisor


def _norm_id(raw) -> int:
    try:
        if raw in (None, "", 0, "0"):
            return 0
        return int(raw)
    except Exception:
        return 0


@dataclass
class GroupCfg:
    group_id: int
    method: str                       # equal_split | equal | award_all | <other>
    method_source: str                # "group" | "group1-default" | "code-default"
    drop_award: int
    drop_divisor: int
    stacks_award_points: bool
    min_pts: int
    max_pts: int
    mods: List[GroupPointMods] = field(default_factory=list)


def _read_config(db, group_id: int, key: str) -> Optional[str]:
    row = (
        db.query(GroupConfiguration.config_value)
        .filter(GroupConfiguration.group_id == group_id, GroupConfiguration.config_key == key)
        .first()
    )
    return row[0] if row and row[0] is not None else None


def _read_config_with_default(db, group_id: int, key: str) -> Tuple[Optional[str], str]:
    """Mirror the engine's fallback: group value -> group 1 default -> None."""
    v = _read_config(db, group_id, key)
    if v is not None:
        return v, "group"
    v = _read_config(db, 1, key)
    if v is not None:
        return v, "group1-default"
    return None, "code-default"


def _truthy(raw: Optional[str]) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"} if raw is not None else False


def _to_int(raw, fallback: int) -> int:
    try:
        return int(raw)
    except Exception:
        return fallback


def load_group_cfg(db, group_id: int) -> GroupCfg:
    # split method (engine default is equal_split when unset)
    method_raw, method_src = _read_config_with_default(db, group_id, "point_sharing_method")
    method = (method_raw or "equal_split").strip().lower()

    drop_row = (
        db.query(GroupPointConfig)
        .filter(GroupPointConfig.group_id == group_id, GroupPointConfig.reason == "drop")
        .first()
    )
    if drop_row is not None:
        award = _to_int(drop_row.award, DEFAULT_DROP_AWARD)
        divisor = _to_int(drop_row.divisor, DEFAULT_DROP_DIVISOR)
        if award <= 0:
            award = DEFAULT_DROP_AWARD
        if divisor == 0:
            divisor = DEFAULT_DROP_DIVISOR
    else:
        award, divisor = DEFAULT_DROP_AWARD, DEFAULT_DROP_DIVISOR

    stacks_raw, _ = _read_config_with_default(db, group_id, "stacks_award_points")
    stacks = _truthy(stacks_raw)

    min_raw, _ = _read_config_with_default(db, group_id, "min_submission_pts")
    max_raw, _ = _read_config_with_default(db, group_id, "max_submission_pts")
    min_pts = max(0, _to_int(min_raw, 0))
    max_pts = max(0, _to_int(max_raw, 0))

    mods = (
        db.query(GroupPointMods)
        .filter(GroupPointMods.group_id == group_id, GroupPointMods.event_type.in_(("drop", "any")))
        .all()
    )
    return GroupCfg(
        group_id=group_id, method=method, method_source=method_src,
        drop_award=award, drop_divisor=divisor, stacks_award_points=stacks,
        min_pts=min_pts, max_pts=max_pts, mods=mods,
    )


def compute_pre_split_award(reason: str, value: int, quantity, item_id, npc_id, cfg: GroupCfg) -> int:
    """Reconstruct ``point_award`` before splitting, mirroring _check_and_award_points."""
    award = cfg.drop_award
    divisor = cfg.drop_divisor if cfg.drop_divisor != 0 else 1
    has_mod = False
    ii, ni = _norm_id(item_id), _norm_id(npc_id)
    for mod in cfg.mods:
        mi, mn = _norm_id(getattr(mod, "item_id", None)), _norm_id(getattr(mod, "npc_id", None))
        item_match = (mi == 0) or (ii != 0 and mi == ii)
        npc_match = (mn == 0) or (ni != 0 and mn == ni)
        met = str(getattr(mod, "event_type", "") or "any").lower()
        if item_match and npc_match and met in (str(reason).lower(), "any"):
            award = _to_int(mod.award, award)
            divisor = _to_int(mod.divisor, divisor) or (cfg.drop_divisor or 1)
            has_mod = True
            break

    qty = None
    try:
        if quantity not in (None, ""):
            q = int(quantity)
            if q > 0:
                qty = q
    except Exception:
        qty = None

    total_value = int(value or 0)
    if has_mod and qty is not None and qty > 0:
        return int(award) * int(qty)
    if has_mod:
        if reason == "drop" and divisor > 1:
            return _round_half_up_div(total_value, divisor) * int(award)
        return int(award)
    if reason == "drop":
        div = divisor if divisor != 0 else 1
        if qty is not None and qty > 1:
            if not cfg.stacks_award_points:
                per_item_value = total_value // qty
                return (_round_half_up_div(per_item_value, div) * int(award)) * qty
            return _round_half_up_div(total_value, div) * int(award)
        return _round_half_up_div(total_value, div) * int(award)
    return int(award)


@dataclass
class RowDelta:
    pp_id: int
    group_id: int
    player_id: int
    role: str            # "target" | "receiver"
    recorded: int
    correct: int
    delta: int           # recorded - correct  (>0 over-credit, <0 under-credit)


@dataclass
class EventResult:
    group_id: int
    entry_id: int
    reason: str
    method: str
    classification: str  # confirmed | award_all | uncertain
    detail: str
    n_targets: int
    point_award: Optional[int]
    rows: List[RowDelta] = field(default_factory=list)


def audit_events(db, group_filter: Optional[int]) -> List[EventResult]:
    # Distinct split events keyed by (group_id, entry_id).
    # Match on the "(received by " marker ALONE: long raid reasons hit the
    # reason column's 125-char cap and get truncated mid-participant-list, so
    # requiring a trailing ")" would silently drop those events.
    split_like = f"%{SPLIT_MARKER}%"
    q = (
        db.query(PlayerPoints.group_id, PlayerPoints.entry_id)
        .filter(PlayerPoints.reason.like(split_like))
        .distinct()
    )
    if group_filter is not None:
        q = q.filter(PlayerPoints.group_id == group_filter)
    events = [(int(g), e) for g, e in q.all() if e is not None]
    # entry_id NULL split rows can't be tied to an event; surface separately.
    null_rows = (
        db.query(PlayerPoints)
        .filter(PlayerPoints.reason.like(split_like), PlayerPoints.entry_id.is_(None))
    )
    if group_filter is not None:
        null_rows = null_rows.filter(PlayerPoints.group_id == group_filter)
    null_count = null_rows.count()

    cfg_cache: Dict[int, GroupCfg] = {}
    results: List[EventResult] = []

    for gid, entry in sorted(events, key=lambda x: (x[0], x[1] or 0)):
        cfg = cfg_cache.get(gid) or cfg_cache.setdefault(gid, load_group_cfg(db, gid))

        rows = (
            db.query(PlayerPoints)
            .filter(PlayerPoints.group_id == gid, PlayerPoints.entry_id == entry)
            .order_by(PlayerPoints.id)
            .all()
        )
        target_rows = [r for r in rows if SPLIT_MARKER in (r.reason or "")]
        base_reason = target_rows[0].reason.split(SPLIT_MARKER, 1)[0]
        recv_rows = [r for r in rows if SPLIT_MARKER not in (r.reason or "") and (r.reason or "") == base_reason]
        n = len(target_rows)
        p = n + 1

        drop = db.query(Drop).filter(Drop.drop_id == entry).first()

        # ---- unrecoverable: no source drop (deleted/purged group) ----
        if drop is None:
            results.append(EventResult(
                group_id=gid, entry_id=entry, reason=base_reason, method=cfg.method,
                classification="uncertain", detail="source drop not found (purged/deleted group)",
                n_targets=n, point_award=None,
            ))
            continue

        pa = compute_pre_split_award(base_reason, drop.value, drop.quantity, drop.item_id, drop.npc_id, cfg)

        # ---- award_all groups are unaffected; sanity-check recorded == pa ----
        if cfg.method not in EQUAL_METHODS:
            recorded_all = [r.amount for r in target_rows] + [r.amount for r in recv_rows]
            capped_pa = _apply_submission_point_bounds(pa, cfg.min_pts, cfg.max_pts)
            matches = all(a == capped_pa for a in recorded_all)
            detail = (f"method={cfg.method} ({cfg.method_source}); recorded==pa"
                      if matches else
                      f"method={cfg.method} ({cfg.method_source}); recorded!=pa (pa={pa}, capped={capped_pa})")
            results.append(EventResult(
                group_id=gid, entry_id=entry, reason=base_reason, method=cfg.method,
                classification="award_all" if matches else "uncertain", detail=detail,
                n_targets=n, point_award=pa,
            ))
            continue

        # ---- equal_split: reconstruct buggy vs fixed ----
        buggy_share = _apply_submission_point_bounds(_ceil_div(pa, p), cfg.min_pts, cfg.max_pts)
        per_target_fixed_raw, receiver_fixed_raw = _compute_split_shares(pa, n, cfg.method)
        fixed_target = _apply_submission_point_bounds(per_target_fixed_raw, cfg.min_pts, cfg.max_pts)
        fixed_receiver = _apply_submission_point_bounds(receiver_fixed_raw, cfg.min_pts, cfg.max_pts)

        # Validate reconstruction: every recorded amount must equal the buggy share.
        reproduces = all(r.amount == buggy_share for r in target_rows) and \
                     all(r.amount == buggy_share for r in recv_rows) and len(recv_rows) >= 1

        row_deltas: List[RowDelta] = []
        for r in target_rows:
            row_deltas.append(RowDelta(int(r.id), gid, int(r.player_id), "target",
                                       int(r.amount), int(fixed_target), int(r.amount) - int(fixed_target)))
        for r in recv_rows:
            row_deltas.append(RowDelta(int(r.id), gid, int(r.player_id), "receiver",
                                       int(r.amount), int(fixed_receiver), int(r.amount) - int(fixed_receiver)))

        over = any(rd.delta > 0 for rd in row_deltas)
        if reproduces and over:
            cls, detail = "confirmed", (
                f"pa={pa} n={n} buggy_share={buggy_share} fixed_target={fixed_target} "
                f"fixed_receiver={fixed_receiver}")
        elif reproduces and not over:
            cls, detail = "no_over_credit", f"pa={pa} n={n} even split (pa%%{p}==0); nothing to correct"
        else:
            cls, detail = "uncertain", (
                f"pa={pa} n={n} buggy_share={buggy_share} but recorded amounts "
                f"{[r.amount for r in target_rows]}/recv{[r.amount for r in recv_rows]} "
                f"do not reproduce buggy ceil (bounds/mod/manual/method-change?)")

        results.append(EventResult(
            group_id=gid, entry_id=entry, reason=base_reason, method=cfg.method,
            classification=cls, detail=detail, n_targets=n, point_award=pa, rows=row_deltas,
        ))

    if null_count:
        print(f"NOTE: {null_count} split target row(s) have NULL entry_id and were skipped (cannot tie to an event).")
    return results


def _player_names(db, ids: List[int]) -> Dict[int, str]:
    if not ids:
        return {}
    out = {}
    for pid, name in db.query(Player.player_id, Player.player_name).filter(Player.player_id.in_(ids)).all():
        out[int(pid)] = name
    return out


def report(db, results: List[EventResult], csv_out: Optional[str], show_all: bool) -> List[RowDelta]:
    by_cls: Dict[str, List[EventResult]] = defaultdict(list)
    for r in results:
        by_cls[r.classification].append(r)

    confirmed = by_cls.get("confirmed", [])
    correctable_rows = [rd for ev in confirmed for rd in ev.rows if rd.delta != 0]
    over_rows = [rd for rd in correctable_rows if rd.delta > 0]
    under_rows = [rd for rd in correctable_rows if rd.delta < 0]

    print("\n================ SPLIT POINT-AWARD AUDIT (dry-run) ================")
    print(f"Split events examined : {len(results)}")
    for cls in ("confirmed", "no_over_credit", "award_all", "uncertain"):
        evs = by_cls.get(cls, [])
        rowc = sum(len(e.rows) for e in evs)
        print(f"  {cls:15}: {len(evs):3} events" + (f", {rowc} rows" if rowc else ""))

    print(f"\nConfirmed over-credited events : {len(confirmed)}")
    print(f"  target rows over-credited    : {len(over_rows)}  "
          f"(total over-credit = {sum(rd.delta for rd in over_rows)} pts)")
    print(f"  receiver rows under-credited : {len(under_rows)} "
          f"(total under-credit = {-sum(rd.delta for rd in under_rows)} pts)")
    net = sum(rd.delta for rd in correctable_rows)
    print(f"  net over-distribution        : {net} pts (removed from leaderboards if fully corrected)")

    # Per-group aggregation
    print("\n-- per-group (confirmed) --")
    gagg: Dict[int, Dict[str, int]] = defaultdict(lambda: {"events": 0, "over": 0, "under": 0, "rows": 0})
    for ev in confirmed:
        gagg[ev.group_id]["events"] += 1
        for rd in ev.rows:
            gagg[ev.group_id]["rows"] += 1
            if rd.delta > 0:
                gagg[ev.group_id]["over"] += rd.delta
            elif rd.delta < 0:
                gagg[ev.group_id]["under"] += -rd.delta
    for gid, a in sorted(gagg.items()):
        print(f"  group {gid}: events={a['events']} over_credit=+{a['over']} under_credit=-{a['under']} rows={a['rows']}")

    # Per-player net delta
    pagg: Dict[Tuple[int, int], int] = defaultdict(int)
    for rd in correctable_rows:
        pagg[(rd.group_id, rd.player_id)] += rd.delta
    names = _player_names(db, [pid for (_, pid) in pagg.keys()])
    print("\n-- per-player net delta (confirmed; +over/-under) --")
    for (gid, pid), d in sorted(pagg.items(), key=lambda kv: (-abs(kv[1]), kv[0])):
        if d != 0:
            print(f"  group {gid} player {pid} ({names.get(pid,'?')}): {d:+d} pts")

    if show_all or by_cls.get("uncertain"):
        print("\n-- uncertain / excluded events (NOT corrected) --")
        for ev in by_cls.get("uncertain", []):
            print(f"  group {ev.group_id} entry {ev.entry_id} [{ev.reason}]: {ev.detail}")
        if show_all:
            for ev in by_cls.get("award_all", []):
                print(f"  group {ev.group_id} entry {ev.entry_id} [award_all]: {ev.detail}")

    if csv_out:
        with open(csv_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["classification", "group_id", "entry_id", "reason", "method",
                        "point_award", "n_targets", "pp_id", "player_id", "role",
                        "recorded_amount", "correct_amount", "delta", "detail"])
            for ev in results:
                if ev.rows:
                    for rd in ev.rows:
                        w.writerow([ev.classification, ev.group_id, ev.entry_id, ev.reason, ev.method,
                                    ev.point_award, ev.n_targets, rd.pp_id, rd.player_id, rd.role,
                                    rd.recorded, rd.correct, rd.delta, ev.detail])
                else:
                    w.writerow([ev.classification, ev.group_id, ev.entry_id, ev.reason, ev.method,
                                ev.point_award, ev.n_targets, "", "", "", "", "", "", ev.detail])
        print(f"\nWrote CSV: {csv_out}")

    return correctable_rows


@dataclass
class Change:
    pp_id: int
    group_id: int
    entry_id: int
    player_id: int
    role: str
    old_amount: int
    new_amount: int


def _collect_changes(results: List[EventResult], mode: str) -> List[Change]:
    """Rows to write: confirmed events only. full = every row that differs from
    its fixed value; over-only = only the over-credited target rows."""
    changes: List[Change] = []
    for ev in results:
        if ev.classification != "confirmed":
            continue
        for rd in ev.rows:
            if rd.delta == 0:
                continue
            if mode == "over-only" and rd.delta <= 0:
                continue
            changes.append(Change(rd.pp_id, ev.group_id, ev.entry_id, rd.player_id,
                                  rd.role, rd.recorded, rd.correct))
    return changes


def apply_corrections(db, results: List[EventResult], mode: str,
                      snapshot_out: str, rollback_out: str) -> int:
    """UPDATE confirmed rows in place to their fixed-engine value.

    Reversible: writes a pre-change snapshot CSV and a rollback .sql BEFORE
    committing, and guards every row with an optimistic check that the live
    amount still equals the audited value (aborts the whole batch on any drift).
    Idempotent: a re-run re-audits first, and already-corrected rows no longer
    reproduce the buggy ceil share, so they drop out of `confirmed`.
    """
    changes = _collect_changes(results, mode)
    if not changes:
        print("\nNothing to apply (no confirmed rows differ from their fixed value).")
        return 0

    # 1) durable snapshot + rollback artifacts, written before any DB write.
    with open(snapshot_out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pp_id", "group_id", "entry_id", "player_id", "role", "old_amount", "new_amount", "delta_applied"])
        for c in changes:
            w.writerow([c.pp_id, c.group_id, c.entry_id, c.player_id, c.role,
                        c.old_amount, c.new_amount, c.new_amount - c.old_amount])
    with open(rollback_out, "w", encoding="utf-8") as fh:
        fh.write("-- Rollback for split point-award correction (mode=%s).\n" % mode)
        fh.write("-- Restores original amounts; run against the `data` schema.\n")
        fh.write("START TRANSACTION;\n")
        for c in changes:
            fh.write(f"UPDATE player_points SET amount = {c.old_amount} WHERE id = {c.pp_id};  "
                     f"-- was corrected to {c.new_amount}\n")
        fh.write("COMMIT;\n")
    print(f"\nSnapshot : {snapshot_out}")
    print(f"Rollback : {rollback_out}")

    # 2) apply inside one transaction with optimistic-concurrency guards.
    print(f"\nApplying {len(changes)} row update(s) (mode={mode}) ...")
    applied = 0
    try:
        for c in changes:
            row = db.query(PlayerPoints).filter(PlayerPoints.id == c.pp_id).with_for_update().first()
            if row is None:
                raise RuntimeError(f"row id={c.pp_id} vanished; aborting")
            if int(row.amount) != c.old_amount:
                raise RuntimeError(
                    f"row id={c.pp_id} amount changed since audit "
                    f"(now {row.amount}, expected {c.old_amount}); aborting whole batch")
            row.amount = c.new_amount  # ORM mutation -> date_updated onupdate fires
            applied += 1
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"ABORTED, rolled back all changes: {e}")
        return 1

    # 3) verify persisted state matches intended fixed values.
    bad = 0
    for c in changes:
        cur = db.query(PlayerPoints.amount).filter(PlayerPoints.id == c.pp_id).scalar()
        if int(cur) != c.new_amount:
            bad += 1
            print(f"  VERIFY MISMATCH id={c.pp_id}: {cur} != {c.new_amount}")
    net = sum(c.new_amount - c.old_amount for c in changes)
    print(f"Applied {applied} update(s); net leaderboard change = {net:+d} pts. "
          f"Verification: {'OK' if bad == 0 else f'{bad} MISMATCH'}")
    return 0 if bad == 0 else 1


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group-id", type=int, default=None, help="Audit only this group_id")
    ap.add_argument("--csv-out", type=str, default=None, help="Write per-row findings to CSV")
    ap.add_argument("--show-all", action="store_true", help="Also list award_all/no-op events")
    ap.add_argument("--apply", action="store_true", help="(GATED) apply corrections - requires --i-understand-this-writes")
    ap.add_argument("--i-understand-this-writes", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--mode", choices=["full", "over-only"], default="full",
                    help="full = restore exact fixed split (targets down, receiver up); "
                         "over-only = only reduce over-credited target rows")
    ap.add_argument("--snapshot-out", type=str,
                    default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "logs", "split_correction_snapshot.csv"),
                    help="Pre-change snapshot CSV (written before applying; disc/logs is gitignored)")
    ap.add_argument("--rollback-out", type=str,
                    default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "logs", "split_correction_rollback.sql"),
                    help="Rollback SQL (written before applying)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    db = Session()
    try:
        results = audit_events(db, args.group_id)
        report(db, results, args.csv_out, args.show_all)

        if args.apply:
            if not args.i_understand_this_writes:
                print("\nREFUSING to apply: pass --i-understand-this-writes to confirm DB writes.")
                return 2
            return apply_corrections(db, results, args.mode, args.snapshot_out, args.rollback_out)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
