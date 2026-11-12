#!/usr/bin/env python3
"""Keep our copy of Wise Old Man's EHP/EHB rate tables in step with theirs.

`utils/efficiency.py` computes EHP and EHB with WOM's algorithm (MIT, ported)
from the tables in ``data/efficiency/rates.json``. WOM revise those tables a
few times a year. This script reads the current ones from their repository,
converts them (as data: nothing downloaded is ever executed) and, if they
differ from ours, replaces the file, proves the result with the efficiency
unit tests, and commits it. A table that fails the tests is rolled back.

    venv/bin/python -m scripts.sync_wom_efficiency            # report only
    venv/bin/python -m scripts.sync_wom_efficiency --apply

Also called from ``scripts/sync_wom_fork`` (the droptracker-wom-sync timer),
which DMs on a change or a failure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

DISC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DISC not in sys.path:
    sys.path.insert(0, DISC)

from utils.js_literal import find_literal  # noqa: E402

REPO = "wise-old-man/wise-old-man"
BRANCH = "master"
RAW = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"
RATES_PATH = os.path.join(DISC, "data", "efficiency", "rates.json")
VENV_PY = os.path.join(DISC, "venv", "bin", "python")
# The upstream commit last compared with ours. WOM commit daily and change
# rates a few times a year: without this every timer run would re-download
# all fifteen files to learn that nothing changed.
CHECKED_STATE = os.path.join(DISC, "var", "wom-efficiency-checked")
VERIFY_TESTS = ["tests/unit/test_efficiency.py"]

BASE = "server/src"
ENUM_FILE = f"{BASE}/types/metric.enum.ts"
PROPS_FILE = f"{BASE}/utils/shared/metric.utils.ts"
CONFIGS = f"{BASE}/api/modules/efficiency/configs"
EHP_VARIANTS = ("main", "ironman", "ultimate", "lvl3", "f2p", "f2p_lvl3", "f2p_ironman",
                "f2p_lvl3_ironman", "def1")
EHB_VARIANTS = ("main", "ironman", "ultimate", "def1")


def log(message: str) -> None:
    print(f"[wom-efficiency] {message}", flush=True)


def remote_head() -> str:
    out = subprocess.run(["git", "ls-remote", f"https://github.com/{REPO}.git", f"refs/heads/{BRANCH}"],
                         capture_output=True, text=True, timeout=60)
    sha = out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else ""
    if len(sha) != 40:
        raise RuntimeError(f"git ls-remote failed: {out.stderr.strip()[-200:]}")
    return sha


def fetch(sha: str, path: str) -> str:
    request = urllib.request.Request(RAW.format(repo=REPO, sha=sha, path=path),
                                     headers={"User-Agent": "sync-wom-efficiency"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def _resolve(value, enums: dict):
    """Turn ``"Skill.ATTACK"`` into ``"attack"`` throughout a parsed table."""
    if isinstance(value, str) and "." in value:
        enum, _, member = value.partition(".")
        if enum in enums:
            if member not in enums[enum]:
                raise ValueError(f"unknown {enum} member {member}")
            return enums[enum][member]
        return value
    if isinstance(value, list):
        return [_resolve(item, enums) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, enums) for key, item in value.items()}
    return value


def build_tables(read) -> dict:
    """Assemble the rates document. ``read(path)`` returns a source file."""
    enum_source = read(ENUM_FILE)
    enums = {
        "Skill": find_literal(enum_source, "export const Skill =")[0],
        "Boss": find_literal(enum_source, "export const Boss =")[0],
    }
    props_source = read(PROPS_FILE)
    skill_props, offset = find_literal(props_source, "= mapValues(")
    boss_marker = props_source.find("= mapValues(", offset)
    boss_props, _ = find_literal(props_source[boss_marker:], "= mapValues(")
    if not any(key.startswith("Boss.") for key in boss_props):
        raise ValueError("could not find the boss properties table")

    tables = {
        "skills": list(enums["Skill"].values()),
        "bosses": list(enums["Boss"].values()),
        "member_skills": [_resolve(key, enums) for key, props in skill_props.items()
                          if props.get("isMembers") is True],
        "f2p_bosses": [_resolve(key, enums) for key, props in boss_props.items()
                       if props.get("isMembers") is False],
        "ehp": {}, "ehb": {},
    }
    for variant in EHP_VARIANTS:
        table = _resolve(find_literal(read(f"{CONFIGS}/ehp/{variant}.ehp.ts"), "export default")[0], enums)
        tables["ehp"][variant] = table
    for variant in EHB_VARIANTS:
        table = _resolve(find_literal(read(f"{CONFIGS}/ehb/{variant}.ehb.ts"), "export default")[0], enums)
        tables["ehb"][variant] = table
    validate(tables)
    return tables


def validate(tables: dict) -> None:
    """Refuse a document the algorithm could not use."""
    skills, bosses = set(tables["skills"]), set(tables["bosses"])
    if "overall" not in skills or len(skills) < 24 or len(bosses) < 60:
        raise ValueError("skill or boss list looks truncated")
    for variant in EHP_VARIANTS:
        metas = tables["ehp"].get(variant) or []
        if not metas:
            raise ValueError(f"ehp/{variant} is empty")
        for meta in metas:
            if meta["skill"] not in skills or not isinstance(meta.get("methods"), list):
                raise ValueError(f"ehp/{variant}: bad skill entry")
            for method in meta["methods"]:
                if not isinstance(method.get("startExp"), (int, float)) \
                        or not isinstance(method.get("rate"), (int, float)):
                    raise ValueError(f"ehp/{variant}/{meta['skill']}: bad method")
            for bonus in meta.get("bonuses") or []:
                if bonus["originSkill"] not in skills or bonus["bonusSkill"] not in skills:
                    raise ValueError(f"ehp/{variant}/{meta['skill']}: bad bonus")
    for variant in EHB_VARIANTS:
        metas = tables["ehb"].get(variant) or []
        if len(metas) < 40:
            raise ValueError(f"ehb/{variant} looks truncated")
        for meta in metas:
            if meta["boss"] not in bosses or not isinstance(meta.get("rate"), (int, float)):
                raise ValueError(f"ehb/{variant}: bad boss entry")


def _checked(sha: str = "") -> str:
    """Read (or, given a sha, record) the upstream commit last compared."""
    try:
        if sha:
            os.makedirs(os.path.dirname(CHECKED_STATE), exist_ok=True)
            with open(CHECKED_STATE, "w") as handle:
                handle.write(sha)
            return sha
        with open(CHECKED_STATE) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def current() -> dict:
    try:
        with open(RATES_PATH) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _tables_only(document: dict) -> dict:
    return {key: value for key, value in document.items() if key != "source"}


def write(document: dict) -> None:
    os.makedirs(os.path.dirname(RATES_PATH), exist_ok=True)
    tmp = RATES_PATH + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(document, handle, indent=1, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, RATES_PATH)


def describe_change(old: dict, new: dict) -> list:
    """Short human lines: which tables changed, and which boss rates."""
    lines = []
    for kind in ("ehp", "ehb"):
        for variant, table in new.get(kind, {}).items():
            before = (old.get(kind) or {}).get(variant)
            if before == table:
                continue
            if kind == "ehb" and before:
                was = {m["boss"]: m["rate"] for m in before}
                now = {m["boss"]: m["rate"] for m in table}
                changed = [f"{boss} {was.get(boss, 'new')} -> {rate}"
                           for boss, rate in now.items() if was.get(boss) != rate]
                gone = [f"{boss} removed" for boss in was if boss not in now]
                lines.append(f"ehb/{variant}: " + ", ".join((changed + gone)[:8]))
            else:
                lines.append(f"{kind}/{variant} changed")
    for key in ("skills", "bosses", "member_skills", "f2p_bosses"):
        if old.get(key) != new.get(key):
            lines.append(f"{key} list changed")
    return lines


def _tests_pass() -> tuple:
    result = subprocess.run([VENV_PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *VERIFY_TESTS],
                            cwd=DISC, capture_output=True, text=True, timeout=600)
    return result.returncode == 0, (result.stdout + result.stderr)[-600:]


def _commit(sha: str, lines: list) -> str:
    git = ["git", "-C", DISC]
    rel = os.path.relpath(RATES_PATH, DISC)
    subprocess.run(git + ["add", "--", rel], capture_output=True)
    message = (f"efficiency: sync EHP/EHB rates with Wise Old Man {sha[:7]}\n\n"
               + "\n".join(lines[:20])
               + "\n\nConverted and verified by scripts/sync_wom_efficiency.py.")
    # Pathspec commit: this tree is shared with other sessions' work.
    if subprocess.run(git + ["commit", "-m", message, "--", rel], capture_output=True).returncode != 0:
        return "rates updated on disk; commit failed"
    branch = subprocess.run(git + ["rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    push = subprocess.run(git + ["push", "origin", branch], capture_output=True, text=True)
    return f"committed + pushed to {branch}" if push.returncode == 0 else "committed locally; push failed"


def sync(apply: bool, *, commit: bool = True) -> dict:
    """Returns ``{"status": "current" | "would_update" | "updated" | "rolled_back" | "error",
    "sha", "changes", "detail"}``. Never raises."""
    try:
        old = current()
        sha = remote_head()
        if old and sha in ((old.get("source") or {}).get("commit"), _checked()):
            return {"status": "current", "sha": sha, "changes": [], "detail": ""}
        tables = build_tables(lambda path: fetch(sha, path))
        changes = describe_change(old, tables)
        if old and not changes:
            # Upstream moved but the tables did not: nothing to do, and no
            # commit just to bump a hash.
            if apply:
                _checked(sha)
            return {"status": "current", "sha": sha, "changes": [], "detail": "tables unchanged"}
        if not apply:
            return {"status": "would_update", "sha": sha, "changes": changes, "detail": ""}
        document = {"source": {"repo": f"https://github.com/{REPO}", "commit": sha,
                               "license": "MIT, Copyright (c) 2020 Ruben Amendoeira",
                               "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                    **tables}
        write(document)
        ok, output = _tests_pass()
        if not ok:
            if old:
                write(old)
            else:
                os.remove(RATES_PATH)
            return {"status": "rolled_back", "sha": sha, "changes": changes, "detail": output}
        detail = _commit(sha, changes) if commit else "written, not committed"
        return {"status": "updated", "sha": sha, "changes": changes, "detail": detail}
    except Exception as exc:
        return {"status": "error", "sha": "", "changes": [], "detail": f"{type(exc).__name__}: {exc}"}


FIXTURE_PATH = os.path.join(DISC, "tests", "unit", "fixtures", "efficiency_parity.json")
_WOM_TYPES = {"regular": None, "ironman": "ironman", "hardcore": "hardcore_ironman",
              "ultimate": "ultimate_ironman", "unknown": None}


def parity(wom_group_id: int, write_fixture: bool) -> int:
    """Compare our EHP/EHB with WOM's for every member of one WOM group (one
    bulk-hiscores call). Run it after a rate change, once the group's members
    have been updated on WOM since: a member WOM last scored under the old
    rates will differ through no fault of ours."""
    import asyncio
    import random

    import db  # noqa: F401  (must load before utils.wiseoldman)
    from utils import efficiency
    from utils.wiseoldman import get_group_bulk_hiscores

    rows = asyncio.run(get_group_bulk_hiscores(wom_group_id)) or []
    players, wrong = [], 0
    for row in rows:
        try:
            player, data = row["player"], row["data"]["data"]
            entry = {
                "type": player["type"], "build": player["build"],
                "skills": {m: v["experience"] for m, v in data["skills"].items()
                           if m != "overall" and v["experience"] > 0},
                "kc": {m: v["kills"] for m, v in data["bosses"].items() if v["kills"] > 0},
                "ehp": data["computed"]["ehp"]["value"], "ehb": data["computed"]["ehb"]["value"],
            }
        except (KeyError, TypeError):
            continue
        ours = efficiency.player_efficiency(entry["skills"], entry["kc"],
                                            account_type=_WOM_TYPES.get(entry["type"]),
                                            build=entry["build"])
        if abs(ours["ehp"] - entry["ehp"]) > 1e-4 or abs(ours["ehb"] - entry["ehb"]) > 1e-4:
            wrong += 1
            entry["stale"] = True
        players.append(entry)
    log(f"group {wom_group_id}: {len(players)} members, {len(players) - wrong} match exactly, {wrong} differ")
    if write_fixture:
        good = [p for p in players if not p.get("stale")]
        random.seed(417)
        special = [p for p in good if not (p["type"] == "regular" and p["build"] == "main")]
        mains = [p for p in good if p["type"] == "regular" and p["build"] == "main"]
        picked = (special + random.sample(mains, max(0, min(len(mains), 200 - len(special)))))[:200]
        with open(FIXTURE_PATH, "w") as handle:
            json.dump({"note": "Real Wise Old Man snapshots (no names) with WOM's own EHP/EHB. "
                               "Valid only against the rates commit below.",
                       "rates_commit": (current().get("source") or {}).get("commit"),
                       "players": picked}, handle, separators=(",", ":"))
        log(f"wrote {len(picked)} players to {os.path.relpath(FIXTURE_PATH, DISC)}")
    return 0 if players and wrong == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write, test and commit")
    parser.add_argument("--no-commit", action="store_true", help="with --apply: leave the commit to you")
    parser.add_argument("--parity", type=int, metavar="WOM_GROUP_ID",
                        help="compare our EHP/EHB with WOM's for one WOM group, then exit")
    parser.add_argument("--write-fixture", action="store_true",
                        help="with --parity: refresh the unit-test fixture from the matching members")
    args = parser.parse_args()
    if args.parity:
        return parity(args.parity, args.write_fixture)
    result = sync(args.apply, commit=not args.no_commit)
    log(f"{result['status']} {result['sha'][:7]} {result['detail']}")
    for line in result["changes"]:
        log(f"  {line}")
    return 0 if result["status"] in ("current", "would_update", "updated") else 1


if __name__ == "__main__":
    sys.exit(main())
