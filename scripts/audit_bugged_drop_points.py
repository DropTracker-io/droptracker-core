#!/usr/bin/env python3
"""
Audit drop point awards affected by the historical rounding-up bugs.

A group's drop divisor ("1 point per 1m") is a MINIMUM as well as a rate, but
two earlier formulas paid out below it: ceil division gave a full point to any
non-zero value, and the half-up division that replaced it gave one to anything
from half the divisor up. Since 2026-09-23 a drop under the divisor earns
nothing, while anything at or above it rounds half-up as it always has.

This script inspects existing `player_points` rows for drop awards and compares:
  - historical logic (`--historical half_up`, the default, or `ceil`)
  - fixed logic (sub-threshold drops pay 0, the rest round half-up)

So it flags only awards that went to drops below the group's minimum. Rows
where the remainder rounded up above the minimum are NOT findings: that is the
intended behaviour and did not change.

It is READ-ONLY: it never writes to `player_points`.

It uses group-specific config from:
  - `group_point_settings` (reason="drop") for award/divisor
  - `group_configurations` (config_key="stacks_award_points")
  - `group_point_mods` (event_type="drop")

Outputs:
  - strict bug matches: recorded amount exactly matches buggy result and differs from fixed
  - potential matches: buggy result differs from fixed and recorded amount is still higher than fixed
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Session, PlayerPoints, Drop, GroupPointConfig, GroupPointMods, GroupConfiguration  # noqa: E402


@dataclass
class DropConfig:
    award: int
    divisor: int
    stacks_award_points: bool


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit drop point awards impacted by the rounding-up bugs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--historical",
        choices=("half_up", "ceil"),
        default="half_up",
        help="Which superseded formula to reconstruct awards with. 'half_up' is "
             "what shipped until 2026-09-23; 'ceil' is the older bug before it.",
    )
    parser.add_argument("--group-id", type=int, default=None, help="Only audit one group_id")
    parser.add_argument("--since-id", type=int, default=None, help="Only rows with player_points.id >= this")
    parser.add_argument("--until-id", type=int, default=None, help="Only rows with player_points.id <= this")
    parser.add_argument("--limit", type=int, default=None, help="Limit rows scanned (most recent first)")
    parser.add_argument(
        "--include-potential",
        action="store_true",
        help="Print potential mismatches in addition to strict bug matches",
    )
    parser.add_argument("--csv-out", type=str, default=None, help="Write findings to CSV file")
    return parser.parse_args(argv)


def _norm_id(raw) -> int:
    try:
        if raw in (None, "", 0, "0"):
            return 0
        return int(raw)
    except Exception:
        return 0


def _ceil_div(value: int, divisor: int) -> int:
    if divisor <= 0:
        return 0
    if value <= 0:
        return 0
    return (value + divisor - 1) // divisor


def _round_half_up_div(value: int, divisor: int) -> int:
    if divisor <= 0:
        return 0
    if value <= 0:
        return 0
    return (value + (divisor // 2)) // divisor


def _threshold_div(value: int, divisor: int) -> int:
    """The live rule: nothing below the divisor, half-up at or above it."""
    if divisor <= 0:
        return 0
    if value < divisor:
        return 0
    return _round_half_up_div(value, divisor)


HISTORICAL_DIV_FNS = {"half_up": _round_half_up_div, "ceil": _ceil_div}


def _is_truthy_config(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    normalized = str(raw).strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _to_positive_int(raw, fallback: int) -> int:
    try:
        value = int(raw)
        return value if value > 0 else fallback
    except Exception:
        return fallback


def load_drop_configs(db_session, group_ids: List[int]) -> Dict[int, DropConfig]:
    configs: Dict[int, DropConfig] = {}

    # group_point_settings fallback defaults match create_default_config()
    default_award = 1
    default_divisor = 10_000_000

    drop_rows = (
        db_session.query(GroupPointConfig)
        .filter(GroupPointConfig.group_id.in_(group_ids), GroupPointConfig.reason == "drop")
        .all()
    )
    drop_map: Dict[int, Tuple[int, int]] = {}
    for row in drop_rows:
        drop_map[int(row.group_id)] = (
            _to_positive_int(row.award, default_award),
            _to_positive_int(row.divisor, default_divisor),
        )

    stack_rows = (
        db_session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id.in_(group_ids),
            GroupConfiguration.config_key == "stacks_award_points",
        )
        .all()
    )
    stacks_by_group: Dict[int, bool] = {}
    for row in stack_rows:
        stacks_by_group[int(row.group_id)] = _is_truthy_config(row.config_value)

    # Mirror point_awards fallback: if missing, use group 1's value, else 0/False.
    group1_stack_row = (
        db_session.query(GroupConfiguration)
        .filter(GroupConfiguration.group_id == 1, GroupConfiguration.config_key == "stacks_award_points")
        .first()
    )
    group1_stack_default = _is_truthy_config(group1_stack_row.config_value) if group1_stack_row else False

    for gid in group_ids:
        award, divisor = drop_map.get(gid, (default_award, default_divisor))
        stacks_award_points = stacks_by_group.get(gid, group1_stack_default)
        configs[gid] = DropConfig(award=award, divisor=divisor, stacks_award_points=stacks_award_points)

    return configs


def load_drop_mods(db_session, group_ids: List[int]) -> Dict[int, List[GroupPointMods]]:
    grouped: Dict[int, List[GroupPointMods]] = defaultdict(list)
    mod_rows = (
        db_session.query(GroupPointMods)
        .filter(GroupPointMods.group_id.in_(group_ids), GroupPointMods.event_type == "drop")
        .order_by(GroupPointMods.id.asc())
        .all()
    )
    for row in mod_rows:
        grouped[int(row.group_id)].append(row)
    return grouped


def compute_points(
    total_value: int,
    quantity: Optional[int],
    item_id: Optional[int],
    npc_id: Optional[int],
    cfg: DropConfig,
    drop_mods: List[GroupPointMods],
    *,
    div_fn,
) -> int:
    award = cfg.award
    divisor = cfg.divisor if cfg.divisor != 0 else 1
    has_mod_override = False

    incoming_item_id = _norm_id(item_id)
    incoming_npc_id = _norm_id(npc_id)

    for mod in drop_mods:
        mod_item_id = _norm_id(getattr(mod, "item_id", None))
        mod_npc_id = _norm_id(getattr(mod, "npc_id", None))
        item_match = (mod_item_id == 0) or (incoming_item_id != 0 and mod_item_id == incoming_item_id)
        npc_match = (mod_npc_id == 0) or (incoming_npc_id != 0 and mod_npc_id == incoming_npc_id)
        if item_match and npc_match:
            try:
                award = int(mod.award)
                has_mod_override = True
            except Exception:
                award = cfg.award
            try:
                divisor = int(mod.divisor) if int(mod.divisor) != 0 else 1
            except Exception:
                divisor = cfg.divisor if cfg.divisor != 0 else 1
            break

    qty = None
    try:
        if quantity not in (None, ""):
            parsed_qty = int(quantity)
            if parsed_qty > 0:
                qty = parsed_qty
    except Exception:
        qty = None

    if has_mod_override and qty is not None and qty > 0:
        return int(award) * int(qty)
    if has_mod_override:
        if divisor > 1:
            return div_fn(int(total_value), int(divisor)) * int(award)
        return int(award)

    # reason="drop" path
    if qty is not None and qty > 1:
        if not cfg.stacks_award_points:
            per_item_value = int(total_value) // int(qty)
            per_item_points = div_fn(int(per_item_value), int(divisor)) * int(award)
            return per_item_points * int(qty)
        return div_fn(int(total_value), int(divisor)) * int(award)

    return div_fn(int(total_value), int(divisor)) * int(award)


def audit(args: argparse.Namespace) -> int:
    historical_div_fn = HISTORICAL_DIV_FNS[args.historical]
    db_session = Session()
    try:
        query = (
            db_session.query(PlayerPoints, Drop)
            .outerjoin(Drop, Drop.drop_id == PlayerPoints.entry_id)
            .filter(PlayerPoints.reason == "drop")
            .filter(PlayerPoints.amount > 0)
        )
        if args.group_id is not None:
            query = query.filter(PlayerPoints.group_id == args.group_id)
        if args.since_id is not None:
            query = query.filter(PlayerPoints.id >= args.since_id)
        if args.until_id is not None:
            query = query.filter(PlayerPoints.id <= args.until_id)

        query = query.order_by(PlayerPoints.id.desc())
        if args.limit is not None and args.limit > 0:
            query = query.limit(args.limit)

        rows: List[Tuple[PlayerPoints, Optional[Drop]]] = query.all()
        if not rows:
            print("No matching drop point rows found.")
            return 0

        group_ids = sorted({int(pp.group_id) for pp, _ in rows if pp.group_id is not None})
        drop_cfgs = load_drop_configs(db_session, group_ids)
        drop_mods = load_drop_mods(db_session, group_ids)

        strict_hits: List[dict] = []
        potential_hits: List[dict] = []
        missing_drop_ref = 0

        for pp, drop in rows:
            if drop is None:
                missing_drop_ref += 1
                continue

            gid = int(pp.group_id)
            cfg = drop_cfgs.get(gid)
            if cfg is None:
                continue

            quantity = drop.quantity if drop.quantity not in (None, "") else 1
            value_each = int(drop.value or 0)
            qty_safe = int(quantity) if int(quantity) > 0 else 1
            total_value = value_each * qty_safe

            buggy_points = compute_points(
                total_value=total_value,
                quantity=drop.quantity,
                item_id=drop.item_id,
                npc_id=drop.npc_id,
                cfg=cfg,
                drop_mods=drop_mods.get(gid, []),
                div_fn=historical_div_fn,
            )
            fixed_points = compute_points(
                total_value=total_value,
                quantity=drop.quantity,
                item_id=drop.item_id,
                npc_id=drop.npc_id,
                cfg=cfg,
                drop_mods=drop_mods.get(gid, []),
                div_fn=_threshold_div,
            )

            if buggy_points <= fixed_points:
                continue

            result = {
                "player_points_id": int(pp.id),
                "group_id": gid,
                "player_id": int(pp.player_id),
                "awarded_amount": int(pp.amount),
                "drop_id": int(drop.drop_id),
                "item_id": int(drop.item_id) if drop.item_id is not None else None,
                "npc_id": int(drop.npc_id) if drop.npc_id is not None else None,
                "drop_value_each": value_each,
                "drop_quantity": int(drop.quantity) if drop.quantity is not None else None,
                "drop_total_value": total_value,
                "group_drop_award": int(cfg.award),
                "group_drop_divisor": int(cfg.divisor),
                "stacks_award_points": 1 if cfg.stacks_award_points else 0,
                "buggy_points": int(buggy_points),
                "fixed_points": int(fixed_points),
                "date_added": str(pp.date_added),
            }

            if int(pp.amount) == int(buggy_points):
                strict_hits.append(result)
            elif int(pp.amount) > int(fixed_points):
                potential_hits.append(result)

        print(f"Historical formula reconstructed: {args.historical}")
        print(f"Rows scanned: {len(rows)}")
        print(f"Rows missing drop reference (entry_id not found in drops): {missing_drop_ref}")
        print(f"Strict bug matches: {len(strict_hits)}")
        print(f"Potential bug matches: {len(potential_hits)}")

        # Per-group rollup: what each clan would decide on. "Overpaid" is the
        # awarded amount minus what floor division would have paid, summed over
        # strict matches (the rows we can attribute to the old formula with
        # certainty); potential matches are counted separately since a boost or
        # a later manual edit can also explain them.
        by_group = defaultdict(lambda: {"strict_rows": 0, "strict_overpaid": 0,
                                        "players": set(), "potential_rows": 0,
                                        "potential_overpaid": 0})
        for row in strict_hits:
            bucket = by_group[row["group_id"]]
            bucket["strict_rows"] += 1
            bucket["strict_overpaid"] += row["awarded_amount"] - row["fixed_points"]
            bucket["players"].add(row["player_id"])
        for row in potential_hits:
            bucket = by_group[row["group_id"]]
            bucket["potential_rows"] += 1
            bucket["potential_overpaid"] += row["awarded_amount"] - row["fixed_points"]
            bucket["players"].add(row["player_id"])

        if by_group:
            print("\nPer-group totals (sorted by points overpaid):")
            ordered = sorted(
                by_group.items(),
                key=lambda kv: kv[1]["strict_overpaid"] + kv[1]["potential_overpaid"],
                reverse=True,
            )
            for gid, bucket in ordered:
                print(
                    f"- group={gid} players={len(bucket['players'])} "
                    f"strict_rows={bucket['strict_rows']} strict_overpaid={bucket['strict_overpaid']} "
                    f"potential_rows={bucket['potential_rows']} "
                    f"potential_overpaid={bucket['potential_overpaid']}"
                )

        if strict_hits:
            print("\nTop strict matches:")
            for row in strict_hits[:50]:
                print(
                    f"- pp_id={row['player_points_id']} group={row['group_id']} drop={row['drop_id']} "
                    f"item={row['item_id']} qty={row['drop_quantity']} total={row['drop_total_value']} "
                    f"awarded={row['awarded_amount']} buggy={row['buggy_points']} fixed={row['fixed_points']}"
                )

        if args.include_potential and potential_hits:
            print("\nTop potential matches:")
            for row in potential_hits[:50]:
                print(
                    f"- pp_id={row['player_points_id']} group={row['group_id']} drop={row['drop_id']} "
                    f"item={row['item_id']} awarded={row['awarded_amount']} "
                    f"buggy={row['buggy_points']} fixed={row['fixed_points']}"
                )

        if args.csv_out:
            out_rows = list(strict_hits)
            if args.include_potential:
                for row in potential_hits:
                    potential_row = dict(row)
                    potential_row["match_type"] = "potential"
                    out_rows.append(potential_row)
            for row in out_rows:
                row.setdefault("match_type", "strict")

            if out_rows:
                fieldnames = [
                    "match_type",
                    "player_points_id",
                    "group_id",
                    "player_id",
                    "awarded_amount",
                    "drop_id",
                    "item_id",
                    "npc_id",
                    "drop_value_each",
                    "drop_quantity",
                    "drop_total_value",
                    "group_drop_award",
                    "group_drop_divisor",
                    "stacks_award_points",
                    "buggy_points",
                    "fixed_points",
                    "date_added",
                ]
                with open(args.csv_out, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(out_rows)
                print(f"\nWrote CSV: {args.csv_out} ({len(out_rows)} rows)")
            else:
                print("\nNo rows to export to CSV.")

        return 0
    finally:
        db_session.close()


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())

