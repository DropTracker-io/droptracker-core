"""Seed site-wide starter event templates from the task generator
(docs/TASK_GENERATOR_PLAN.md, step 4).

Each starter is a public, site-wide ``web_event_templates`` row (group_id
NULL) whose tasks come from ``services.task_generator`` with a fixed seed, so
re-running produces the same template. Clans pick them from "Start from a
template" in the create form, then tune tasks like any other event.

Dry-run by default (prints every template's tasks); ``--apply`` upserts by
name. Idempotent: an existing starter of the same name is overwritten, never
duplicated.

    venv/bin/python -m scripts.seed_starter_templates
    venv/bin/python -m scripts.seed_starter_templates --apply
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")

BALANCED = {"air": 3, "water": 3, "earth": 2, "fire": 1}

STARTERS = (
    {
        "name": "Weekend bingo (5x5)",
        "description": "A 25-tile board for a short weekend event. Sized for teams of about 5.",
        "kind": "bingo", "board_size": 5, "count": 25, "days": 2.5, "team_size": 5,
        "mix": BALANCED, "bonus_line_points": 5, "bonus_blackout_points": 25, "seed": 5501,
    },
    {
        "name": "Two-week bingo (7x7)",
        "description": "A 49-tile board for a longer clan event. Sized for teams of about 10.",
        "kind": "bingo", "board_size": 7, "count": 49, "days": 14, "team_size": 10,
        "mix": BALANCED, "bonus_line_points": 10, "bonus_blackout_points": 50, "seed": 7714,
    },
    {
        "name": "PvM week",
        "description": "Boss uniques, kill counts, pets and combat achievements over one week.",
        "kind": "standard", "count": 15, "days": 7, "team_size": 5, "mix": BALANCED,
        "kinds": {"uniques", "kc", "pets", "ca"}, "seed": 1507,
    },
    {
        "name": "Skilling week",
        "description": "Skill XP goals and slayer tasks over one week.",
        "kind": "standard", "count": 12, "days": 7, "team_size": 5, "mix": BALANCED,
        "kinds": {"xp", "slayer"}, "categories": {"skilling", "slayer"}, "seed": 1207,
    },
)


def build_payload(spec: dict, tasks: list[dict]) -> dict:
    """The template payload (services' snapshot shape, schema version 1)."""
    bingo = spec["kind"] == "bingo"
    size = int(spec.get("board_size") or 5)
    return {
        "version": 1,
        "event": {
            "description": spec["description"],
            "formation_mode": "self_join",
            "requires_confirmation": False,
            "submission_policy": "all",
            "has_bingo": bingo,
            "board_size": size,
            "bonus_line_points": int(spec.get("bonus_line_points") or 0),
            "bonus_blackout_points": int(spec.get("bonus_blackout_points") or 0),
            "mode": "standard",
            "kind": spec["kind"],
            "buyins_enabled": False,
            "prize_config": None,
            "leadership_config": None,
            "message_config": None,
            "schedule": None,
            "clan_points": None,
        },
        "tasks": [
            {
                "type": t["type"],
                "label": t["label"],
                "target": t.get("target") or None,
                "target_value": t.get("target_value"),
                "points": int(t.get("points") or 0),
                "requires_confirmation": False,
                "visibility": "private",
                "config": t.get("config"),
                "difficulty": t.get("difficulty"),
            }
            for t in tasks
        ],
        "teams": [{"name": "Team 1"}, {"name": "Team 2"}],
        "bingo": ({"size": size,
                   "cells": [{"idx": i, "label": tasks[i]["label"] if i < len(tasks) else "",
                              "task_ref": i if i < len(tasks) else None}
                             for i in range(size * size)]}
                  if bingo else None),
    }


def spread(rows: list[dict], seed: int) -> list[dict]:
    """Shuffle a bingo board so difficulty isn't banded row by row."""
    import random

    out = list(rows)
    random.Random(seed).shuffle(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the templates")
    args = ap.parse_args()

    from db import EventTemplate
    from db.models.base import Session
    from services import task_generator as tg
    from web_api.common import ProblemException
    from web_api.routes.event_task_validation import validate_task_payload
    from web_api.task_generator_catalog import assemble_catalog

    s = Session()
    try:
        catalog = assemble_catalog(s)
        if not catalog:
            print("Catalog is empty (clan log / npc tables unreadable); aborting.")
            return 1
        planned = []
        for spec in STARTERS:
            res = tg.generate(
                catalog, count=spec["count"],
                capacity=tg.capacity_hours(spec["team_size"], spec["days"]),
                mix=spec["mix"], seed=spec["seed"],
                categories=spec.get("categories"), kinds=spec.get("kinds"),
            )
            tasks = []
            for row in res["tasks"]:
                try:
                    validate_task_payload(s, json.loads(json.dumps(row["task"])))
                    tasks.append(row["task"])
                except ProblemException:
                    print(f"  (skipping invalid generated task {row['task']['label']!r})")
            if spec["kind"] == "bingo":
                tasks = spread(tasks, spec["seed"])
            planned.append((spec, tasks, res))
        s.rollback()

        for spec, tasks, res in planned:
            print(f"\n== {spec['name']} ({spec['kind']}, {len(tasks)}/{spec['count']} tasks, "
                  f"~{res['total_hours']}h of ~{res['capacity_hours']}h capacity)")
            for t in tasks:
                print(f"   [{tg.DIFFICULTY_LABELS[t['difficulty']]:<6}] {t['label']}")

        if not args.apply:
            print("\nDry run. Re-run with --apply to write these templates.")
            return 0

        for spec, tasks, _res in planned:
            payload = build_payload(spec, tasks)
            row = (s.query(EventTemplate)
                   .filter(EventTemplate.group_id.is_(None), EventTemplate.name == spec["name"])
                   .first())
            if row is None:
                row = EventTemplate(name=spec["name"], group_id=None)
                s.add(row)
            row.description = spec["description"]
            row.source_event_id = None
            row.created_by_user_id = None
            row.visibility = "public"
            row.mode = "standard"
            row.has_bingo = payload["event"]["has_bingo"]
            row.board_size = payload["event"]["board_size"]
            row.task_count = len(payload["tasks"])
            row.team_count = len(payload["teams"])
            row.schema_version = 1
            row.payload = json.dumps(payload)
            row.active = True
        s.commit()
        print(f"\nWrote {len(planned)} starter template(s).")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
