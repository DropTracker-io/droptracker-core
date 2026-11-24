"""Build the quest id -> name map the Data API and player profiles use.

``player_quest_states`` is keyed by the game's quest id. The names come from
RuneLite's ``Quest`` enum, which is also the list the plugin polls, so the two
always agree on what an id means. This reads the enum's source and writes
``data/quests.json`` (committed, so a change is reviewable).

The ``retired`` list in the file is kept as it is: those are ids the game no
longer uses, whose old rows must not be counted (see ``utils/quests.py``). An
id that drops out of the enum is reported so it can be added there by hand.

Dry-run by default; ``--write`` saves the file. Run it when a quest ships::

    ./venv/bin/python -m scripts.sync_quest_names
    ./venv/bin/python -m scripts.sync_quest_names --write
    ./venv/bin/python -m scripts.sync_quest_names --source /path/to/Quest.java
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "quests.json")
SOURCE_URL = ("https://raw.githubusercontent.com/runelite/runelite/master/"
              "runelite-api/src/main/java/net/runelite/api/Quest.java")

# `	DORICS_QUEST(30, "Doric's Quest"),`
_CONSTANT_RE = re.compile(r'^\s*[A-Z][A-Z0-9_]*\((\d+),\s*"((?:[^"\\]|\\.)*)"\)[,;]', re.M)


def parse(source: str) -> list:
    quests = [{"id": int(m.group(1)), "name": m.group(2).replace('\\"', '"')}
              for m in _CONSTANT_RE.finditer(source)]
    return sorted(quests, key=lambda q: q["id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="a local Quest.java instead of GitHub")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    if args.source:
        with open(args.source, encoding="utf-8") as fh:
            source = fh.read()
    else:
        request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "quest-name-sync"})
        with urllib.request.urlopen(request, timeout=30) as response:
            source = response.read().decode("utf-8")

    quests = parse(source)
    if len(quests) < 150:
        print(f"Only {len(quests)} quests parsed; the enum's format changed. Not writing.")
        return 1

    try:
        with open(OUT_PATH, encoding="utf-8") as fh:
            current = json.load(fh)
    except (OSError, ValueError):
        current = {}
    before = {int(q["id"]): q["name"] for q in current.get("quests", [])}
    after = {q["id"]: q["name"] for q in quests}
    retired = sorted(int(i) for i in current.get("retired", []))

    for quest_id in sorted(set(after) - set(before)):
        print(f"  + {quest_id}  {after[quest_id]}")
    for quest_id in sorted(set(before) - set(after)):
        print(f"  - {quest_id}  {before[quest_id]}  (gone from the enum: add to 'retired' if renumbered)")
    for quest_id in sorted(set(before) & set(after)):
        if before[quest_id] != after[quest_id]:
            print(f"  ~ {quest_id}  {before[quest_id]} -> {after[quest_id]}")
    print(f"{len(quests)} quests, {len(retired)} retired ids.")

    if not args.write:
        print("Dry run. --write saves data/quests.json.")
        return 0
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"source": "RuneLite net.runelite.api.Quest", "retired": retired,
                   "quests": quests}, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    print(f"Wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
