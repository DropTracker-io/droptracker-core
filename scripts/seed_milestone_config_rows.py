"""Seed the template group's (group 1) milestone config rows.

New-group defaults come from CLONING group 1's group_configurations rows
(db/group_creation.py), not from the registry defaults — so a new config key
needs a group-1 row or every group created after it silently reads nothing.
Existing groups are untouched: the processors fall back to the code defaults
when a row is absent, and both milestone families default OFF.

Seeds the ten keys of the KC-milestone + hiscores-rank-milestone families
(web_api/config_registry.py, "milestones" category + the two channel keys)
with their registry defaults. Idempotent: existing group-1 rows are never
overwritten.

Dry-run by default; --apply to write.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.seed_milestone_config_rows [--apply]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import GroupConfiguration, Session  # noqa: E402
from web_api.config_registry import coerce_to_storage, get_config_field  # noqa: E402

TEMPLATE_GROUP_ID = 1

#: The milestone-family keys, seeded with their registry defaults.
MILESTONE_KEYS = (
    "channel_id_to_post_kc",
    "channel_id_to_post_ranks",
    "notify_kc_milestones",
    "notify_first_kc",
    "kc_milestone_interval",
    "notify_rank_milestones",
    "rank_milestone_thresholds",
    "rank_milestone_bosses",
    "rank_milestone_skills",
    "rank_milestone_clues",
)


def seed(session, apply: bool = False) -> dict:
    created, skipped = [], []
    existing = {
        row.config_key
        for row in session.query(GroupConfiguration.config_key)
        .filter(
            GroupConfiguration.group_id == TEMPLATE_GROUP_ID,
            GroupConfiguration.config_key.in_(MILESTONE_KEYS),
        )
        .all()
    }
    for key in MILESTONE_KEYS:
        if key in existing:
            skipped.append(key)
            continue
        field = get_config_field(key)
        if field is None:
            raise SystemExit(f"{key} is not in the config registry — seed aborted")
        default = field.get("default")
        stored = coerce_to_storage(key, default) if default is not None else ""
        created.append((key, stored))
        if apply:
            session.add(
                GroupConfiguration(
                    group_id=TEMPLATE_GROUP_ID,
                    config_key=key,
                    config_value=stored,
                )
            )
    if apply and created:
        session.commit()
    return {"created": created, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the rows (default: dry run)")
    args = parser.parse_args()

    with Session() as session:
        result = seed(session, apply=args.apply)

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"[{mode}] template group {TEMPLATE_GROUP_ID}:")
    for key, stored in result["created"]:
        print(f"  + {key} = {stored!r}")
    for key in result["skipped"]:
        print(f"  = {key} (already present, untouched)")
    if not args.apply and result["created"]:
        print("Re-run with --apply to write.")


if __name__ == "__main__":
    main()
