"""Switch monthly recaps on for a cohort of clans, and switch them back off.

This is how the first clan cards get authorised. The delivery job asks one
question of each group — is ``recaps_enabled`` on? — and nothing else, so
there's no "first one is free" branch hiding in the code: a clan that doesn't
want a recap can turn the setting off before the 1st and never receive one.

The launch pass sets it for the busiest clans, they get their card, and
``--revert`` turns it back off so the ones who want more have to say so. That
revert is only safe because of the marker: seeding writes ``recaps_seeded``
alongside, and ``web_api/routes/config.py`` deletes that marker the moment an
admin edits ``recaps_enabled`` themselves. So the revert can run blind — it
skips any clan that has taken ownership of the setting, which is precisely the
clan you least want to switch off.

Ranking is by the month's loot, summed from the same Redis totals the card's
headline figure uses, so the cohort is "the clans this recap is actually about".
Groups with nowhere to post are excluded: a clan with no recap channel and no
lootboard channel would be enabled for nothing.

Usage
-----
    # who would be seeded? (writes nothing)
    python -m scripts.seed_recap_groups --period 2026-07

    # arm the top 100 for the launch
    python -m scripts.seed_recap_groups --period 2026-07 --apply

    # after the cards have gone out
    python -m scripts.seed_recap_groups --revert --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db import Session  # noqa: E402
from services.recap_delivery import (  # noqa: E402
    CFG_CHANNEL,
    CFG_ENABLED,
    CFG_LOOTBOARD_CHANNEL,
    last_completed_month,
)

SEED_MARKER = "recaps_seeded"
DEFAULT_COHORT = 100


def _set_config(session, group_id: int, key: str, value: str) -> None:
    session.execute(
        text(
            "INSERT INTO group_configurations (group_id, config_key, config_value) "
            "VALUES (:gid, :key, :val) ON DUPLICATE KEY UPDATE config_value = :val"
        ),
        {"gid": group_id, "key": key, "val": value},
    )


def _candidates(session, period: str) -> list[tuple[int, str, int, int]]:
    """``(group_id, name, members, loot)`` for every clan that could receive a
    card, richest month first."""
    from services.recap import _redis_totals, _visible_group_player_ids, period_partition

    partition = period_partition(period)
    rows = session.execute(
        text("SELECT group_id, group_name FROM groups WHERE group_id NOT IN (1, 2)")
    ).fetchall()

    # One bulk config read rather than two per group.
    channel_rows = session.execute(
        text(
            "SELECT group_id, config_key, config_value FROM group_configurations "
            "WHERE config_key IN (:a, :b)"
        ),
        {"a": CFG_CHANNEL, "b": CFG_LOOTBOARD_CHANNEL},
    ).fetchall()
    channels: dict[int, str] = {}
    for gid, key, value in channel_rows:
        value = (value or "").strip()
        if not value:
            continue
        # An explicit recap channel wins over the lootboard fallback.
        if key == CFG_CHANNEL or int(gid) not in channels:
            channels[int(gid)] = value

    out = []
    for group_id, name in rows:
        group_id = int(group_id)
        if group_id not in channels:
            continue
        members = _visible_group_player_ids(session, group_id)
        if not members:
            continue
        totals = _redis_totals(members, partition)
        loot = sum(int(v or 0) for v in totals.values())
        if loot <= 0:
            continue
        out.append((group_id, name or f"Group {group_id}", len(members), loot))

    out.sort(key=lambda r: r[3], reverse=True)
    return out


def _gp(value: int) -> str:
    for unit, size in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(value) >= size:
            return f"{value / size:.2f}{unit}"
    return str(value)


def do_seed(session, period: str, cohort: int, apply: bool) -> int:
    candidates = _candidates(session, period)[:cohort]
    print(f"  {len(candidates)} clan(s) in the cohort (ranked by {period} loot)")
    for rank, (group_id, name, members, loot) in enumerate(candidates, start=1):
        if rank <= 15 or rank == len(candidates):
            print(f"    {rank:>3}. {name[:32]:<32} {members:>4} members  {_gp(loot):>9}")
        elif rank == 16:
            print(f"    ... {len(candidates) - 16} more ...")
        if apply:
            _set_config(session, group_id, CFG_ENABLED, "1")
            _set_config(session, group_id, SEED_MARKER, period)
    if apply:
        session.commit()
        _invalidate([c[0] for c in candidates])
    return len(candidates)


def do_revert(session, apply: bool) -> int:
    """Switch off only what a seeding pass set and nobody has touched since."""
    rows = session.execute(
        text(
            "SELECT s.group_id, g.group_name, s.config_value "
            "FROM group_configurations s "
            "JOIN group_configurations e "
            "  ON e.group_id = s.group_id AND e.config_key = :enabled "
            "LEFT JOIN groups g ON g.group_id = s.group_id "
            "WHERE s.config_key = :marker AND e.config_value = '1'"
        ),
        {"marker": SEED_MARKER, "enabled": CFG_ENABLED},
    ).fetchall()
    print(f"  {len(rows)} clan(s) still carry the seed marker")
    for group_id, name, seeded_period in rows:
        print(f"    {name or group_id} (seeded {seeded_period})")
        if apply:
            _set_config(session, int(group_id), CFG_ENABLED, "0")
            session.execute(
                text(
                    "DELETE FROM group_configurations "
                    "WHERE group_id = :gid AND config_key = :marker"
                ),
                {"gid": int(group_id), "marker": SEED_MARKER},
            )
    if apply:
        session.commit()
        _invalidate([int(r[0]) for r in rows])
    return len(rows)


def _invalidate(group_ids: list[int]) -> None:
    """Drop the bot-side config cache so a seeded clan is visible immediately."""
    try:
        import utils.group_config as gc

        for group_id in group_ids:
            gc.invalidate(group_id)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed / revert clan recap opt-ins.")
    ap.add_argument("--period", help="'YYYY-MM' to rank by (default: last completed month)")
    ap.add_argument("--cohort", type=int, default=DEFAULT_COHORT, help="how many clans")
    ap.add_argument("--revert", action="store_true", help="switch off what seeding set")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    period = (args.period or last_completed_month()).strip()
    mode = "APPLY" if args.apply else "DRY RUN"
    action = "revert" if args.revert else "seed"
    print(f"[{mode}] recap {action}" + ("" if args.revert else f" for {period}"))

    session = Session()
    try:
        count = (
            do_revert(session, args.apply)
            if args.revert
            else do_seed(session, period, args.cohort, args.apply)
        )
    finally:
        session.close()

    verb = "would affect" if not args.apply else "affected"
    print(f"[{mode}] {verb} {count} clan(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
