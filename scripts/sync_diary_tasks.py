"""Build the achievement diary task list the player profile renders.

The plugin syncs how many tasks a player has done per diary tier, and the
site shows the diary the way the game's journal does: every task, by tier.
The game publishes no task list a server can read, so this scrapes the OSRS
Wiki's twelve diary articles into ``data/diary_tasks.json`` (committed, so a
change is reviewable).

**Task order is load-bearing.** The wiki's WikiSync data reports a player's
tasks as a list of booleans in the order of these same tables, and
``services/diary_tasks.py`` pairs the two by position.

Dry-run by default; ``--write`` saves the file::

    ./venv/bin/python -m scripts.sync_diary_tasks
    ./venv/bin/python -m scripts.sync_diary_tasks --write
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "diary_tasks.json")

# area_id as the game and `player_diary_tiers` number them -> (display name,
# wiki article, the name WikiSync keys the area by).
AREAS = [
    (0, "Karamja", "Karamja Diary", "Karamja"),
    (1, "Ardougne", "Ardougne Diary", "Ardougne"),
    (2, "Falador", "Falador Diary", "Falador"),
    (3, "Fremennik", "Fremennik Diary", "Fremennik"),
    (4, "Kandarin", "Kandarin Diary", "Kandarin"),
    (5, "Desert", "Desert Diary", "Desert"),
    (6, "Lumbridge & Draynor", "Lumbridge & Draynor Diary", "Lumbridge & Draynor"),
    (7, "Morytania", "Morytania Diary", "Morytania"),
    (8, "Varrock", "Varrock Diary", "Varrock"),
    (9, "Wilderness", "Wilderness Diary", "Wilderness"),
    (10, "Western Provinces", "Western Provinces Diary", "Western Provinces"),
    (11, "Kourend & Kebos", "Kourend & Kebos Diary", "Kourend & Kebos"),
]
TIERS = ["Easy", "Medium", "Hard", "Elite"]

# What the game's own [proc,diary_completion_info] script says each tier holds,
# by area_id. The scrape must agree with it or the file is not written: a
# miscounted table would shift every later task onto the wrong completion bit.
EXPECTED_COUNTS = {
    0: (10, 19, 10, 5), 1: (10, 12, 12, 8), 2: (11, 14, 11, 6),
    3: (10, 9, 9, 6), 4: (11, 14, 11, 7), 5: (11, 12, 10, 6),
    6: (12, 12, 11, 6), 7: (11, 11, 10, 6), 8: (14, 13, 10, 5),
    9: (12, 11, 10, 7), 10: (11, 13, 13, 7), 11: (12, 13, 10, 8),
}

_TABLE_RE = re.compile(
    r'\{\|[^\n]*data-diary-name="([^"]+)"[^\n]*data-diary-tier="([^"]+)"[^\n]*\n(.*?)\n\|\}',
    re.S,
)


def _strip_templates(text: str) -> str:
    """Drop ``{{...}}`` templates, innermost first, keeping what reads as text."""
    def repl(match):
        parts = match.group(1).split("|")
        name = parts[0].strip().lower()
        args = [p for p in parts[1:] if "=" not in p]
        if name == "scp":
            # {{SCP|Thieving|5|link=y}} -> "5 Thieving"; {{SCP|Quest}} -> "".
            if len(args) >= 2:
                return f"{args[1].strip()} {args[0].strip()}"
            return ""
        if name in ("fairycode",):
            return args[0].strip().upper() if args else ""
        if name in ("coins", "nowrap", "nw"):
            return args[0].strip() if args else ""
        return ""

    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\{\{([^{}]*)\}\}", repl, text)
    return text


def clean(text: str) -> str:
    """Wikitext -> the plain sentence a reader sees."""
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = _strip_templates(text)
    text = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", text)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("'''", "").replace("''", "").replace(" ()", "")
    return re.sub(r"\s+", " ", text).strip(" \t\n*:")


def parse_tasks(table_body: str) -> list:
    tasks = []
    for row in re.split(r"\n\|-[^\n]*", "\n" + table_body):
        cells = [c for c in re.split(r"\n\|(?!\})", "\n" + row.strip()) if c.strip()]
        if not cells:
            continue
        first = cells[0].lstrip("|").strip()
        match = re.match(r"(\d+)\.\s*(.*)", first, re.S)
        if not match:
            continue
        lines = match.group(2).split("\n")
        description = clean(lines[0])
        notes = [clean(line) for line in lines[1:]]
        notes = [re.sub(r"^Note:\s*", "", n) for n in notes if n]
        requirements = []
        if len(cells) > 1:
            for line in cells[1].split("\n"):
                line = line.strip()
                if not line.startswith("*"):
                    continue
                req = clean(line)
                if req and req.lower() not in ("none", "n/a"):
                    requirements.append(req)
        task = {"description": description}
        if requirements:
            task["requirements"] = requirements
        if notes:
            task["note"] = " ".join(notes)
        tasks.append(task)
    return tasks


def fetch(title: str, user_agent: str) -> str:
    url = "https://oldschool.runescape.wiki/w/" + urllib.parse.quote(title.replace(" ", "_")) + "?action=raw"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def build(user_agent: str) -> tuple[dict, list]:
    problems = []
    areas = []
    for area_id, name, article, wikisync in AREAS:
        text = fetch(article, user_agent)
        found = {tier: parse_tasks(body) for _, tier, body in _TABLE_RE.findall(text)}
        tiers = []
        for index, tier in enumerate(TIERS):
            tasks = found.get(tier, [])
            expected = EXPECTED_COUNTS[area_id][index]
            if len(tasks) != expected:
                problems.append(f"{name} {tier}: scraped {len(tasks)} tasks, the game has {expected}")
            if any(not t["description"] for t in tasks):
                problems.append(f"{name} {tier}: a task with no text")
            tiers.append({"tier": index, "name": tier, "tasks": tasks})
        areas.append({"area_id": area_id, "name": name, "wikisync": wikisync, "tiers": tiers})
        time.sleep(0.5)
    return {"source": "oldschool.runescape.wiki", "areas": areas}, problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="save data/diary_tasks.json")
    args = parser.parse_args(argv)

    from utils.wiki_ua import USER_AGENT

    data, problems = build(USER_AGENT)
    total = sum(len(t["tasks"]) for a in data["areas"] for t in a["tiers"])
    for area in data["areas"]:
        print(f'{area["name"]:<22}' + "  ".join(f'{t["name"]} {len(t["tasks"])}' for t in area["tiers"]))
    print(f"{total} tasks")
    for problem in problems:
        print("PROBLEM:", problem)
    if problems:
        print("Not written: the scrape disagrees with the game's task counts.")
        return 1
    if not args.write:
        print("Dry run. Pass --write to save", OUT_PATH)
        return 0
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    print("Wrote", OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
