"""Generate the 'Loot Sweep (All Content)' template config from the sheet.

Reads gviz.csv, resolves item + NPC names against the DB, folds meta-sets
(Barrows/DKs/Moons) into one task with groups, collapses the sheet's duplicate
"(1,3,5,7,9)" rows into one item with awards_per_tier, and writes:
  - loot_sweep_template.json  (the tasks, ready for a template payload)
  - report.txt                (everything skipped / needing human review)

Does NOT touch prod. Pets are skipped (they arrive as `pet` submissions, not
NPC-scoped drops, so loot_sweep can't credit them) and reported.
"""
import csv, json, os, re
from collections import defaultdict
from dotenv import load_dotenv
import pymysql

SP = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SP, "..", "..", ".env"))
conn = pymysql.connect(host="localhost", port=3306, user=os.getenv("DB_USER"),
                       password=os.getenv("DB_PASS"), database="data")
cur = conn.cursor()
cur.execute("SELECT item_name FROM items WHERE item_name IS NOT NULL")
ITEM_BY_NORM = {}
for (n,) in cur.fetchall():
    ITEM_BY_NORM.setdefault(" ".join(n.strip().lower().split()), n)
cur.execute("SELECT npc_name FROM npc_list WHERE npc_name IS NOT NULL")
NPC_BY_NORM = {}
for (n,) in cur.fetchall():
    NPC_BY_NORM.setdefault(" ".join(n.strip().lower().split()), n)
# id -> canonical name (the sheet's shorthand names are unreliable; its ids
# bridge to the real name even though the ids themselves aren't stored).
cur.execute("SELECT item_id, item_name FROM items WHERE item_name IS NOT NULL")
NAME_BY_ID = {int(i): n for i, n in cur.fetchall()}
conn.close()


def norm(s):
    return " ".join(str(s or "").strip().lower().split())


def resolve_item(name):
    return ITEM_BY_NORM.get(norm(name))


# Boss header -> canonical NpcList name(s), for headers that don't match the DB
# spelling directly. Multi-source sets (Wilderness wards, Slayer, Misc, raids,
# Revenants, Champion scrolls) are deliberately left for human review.
NPC_ALIAS = {
    "ahrim": "Ahrim the Blighted", "dharok": "Dharok the Wretched",
    "guthan": "Guthan the Infested", "karil": "Karil the Tainted",
    "torag": "Torag the Corrupted", "verac": "Verac the Defiled",
    "demonic gorillas": "Demonic gorilla", "hueycoatl": "The Hueycoatl",
    "nightmare": "The Nightmare", "thermonuclear devil": "Thermonuclear smoke devil",
    "gauntlet": "Crystalline Hunllef", "vet'ion": "Vet'ion",
    "kraken (boss)": "Kraken", "phantom muspah": "Phantom Muspah",
    "grotesque guardians": "Dusk", "dagannoth supreme": "Dagannoth Supreme",
    "dagannoth rex": "Dagannoth Rex", "dagannoth prime": "Dagannoth Prime",
    "eclipse moon": "Eclipse Moon", "blood moon": "Blood Moon", "blue moon": "Blue Moon",
    "chambers of xeric": "Chambers of Xeric", "tombs of amascut": "Tombs of Amascut",
}


def resolve_npc(name):
    a = NPC_ALIAS.get(norm(name))
    if a and resolve_npc_raw(a):
        return resolve_npc_raw(a)
    return resolve_npc_raw(name)


def resolve_npc_raw(name):
    return NPC_BY_NORM.get(norm(name))


# ── parse the sheet ──────────────────────────────────────────────────────────
rows = list(csv.reader(open(f"{SP}/all_content_sheet.csv", newline="")))

def cell(r, i):
    return r[i].strip() if i < len(r) else ""

def to_int(s):
    s = s.replace(",", "").strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None

sets = []
cur_set = None
for idx in range(3, len(rows)):
    r = rows[idx]
    iid = to_int(cell(r, 0)); ge = cell(r, 4); pts = to_int(cell(r, 5)); name = cell(r, 6)
    if not name:
        continue
    lname = name.lower()
    has_id = iid is not None
    is_header = (not has_id and not ge and name[:1].isupper() and pts and pts > 0
                 and "(bonus)" not in lname and not lname.startswith("pet") and " pet" not in lname)
    if is_header:
        cur_set = {"name": name, "set_bonus": pts, "items": []}
        sets.append(cur_set)
        continue
    if (pts is None or pts == 0) and not has_id:
        continue  # 'any X' / '#N' label rows — skipped (reported separately)
    if cur_set is None:
        continue
    is_pet = lname.startswith("pet") or " pet" in lname
    is_bonus = "(bonus)" in lname or is_pet
    cur_set["items"].append({
        "name": name.strip(), "points": pts, "counts": not is_bonus,
        "is_pet": is_pet, "id": iid,
    })

# ── meta-sets: fold sub-sets into one task ───────────────────────────────────
META = {
    "Barrows Brothers": ["Ahrim", "Dharok", "Guthan", "Karil", "Torag", "Verac"],
    "Dagannoth Kings": ["Dagannoth Supreme", "Dagannoth Rex", "Dagannoth Prime"],
    "Moons of Peril": ["Eclipse Moon", "Blood Moon", "Blue Moon"],
}
by_name = {s["name"]: s for s in sets}
child_names = {c for kids in META.values() for c in kids}

report = []
tasks = []


def base_item_name(n):
    # strip a trailing "(1, 3, 5, 7, 9)" style receipt-tier suffix
    return re.sub(r"\s*\(\s*\d+(?:\s*,\s*\d+)*\s*\)\s*$", "", n).strip()


def build_group(label, npc_names, bonus, raw_items):
    """One group from a set's raw item rows: resolve items, collapse batched
    duplicate rows (awards_per_tier), skip+report pets/unknowns."""
    npcs = []
    for nn in npc_names:
        c = resolve_npc(nn)
        if c and c not in npcs:
            npcs.append(c)
        elif not c:
            report.append(f"NPC unresolved: '{nn}' (group '{label}')")
    # collapse duplicate rows by base name
    batches = defaultdict(list)
    order = []
    for it in raw_items:
        if it["is_pet"]:
            report.append(f"PET skipped (pet submissions aren't NPC drops): '{label}' {it['points']}pts")
            continue
        b = base_item_name(it["name"])
        if b not in batches:
            order.append(b)
        batches[b].append(it)
    items = []
    for b in order:
        grp = batches[b]
        # Prefer the sheet's id -> canonical DB name (its shorthand names don't
        # match); fall back to resolving the (de-suffixed) name.
        # Resolve by NAME first — the sheet's real item names are correct even
        # where its ids are wrong (6 reused ids). Fall back to id->name only for
        # shorthand names ("hood", "odium 1") that don't match the DB.
        iid = grp[0].get("id")
        canon = resolve_item(b) or resolve_item(b.rstrip("s"))
        if not canon and iid is not None:
            canon = NAME_BY_ID.get(iid)
        if not canon:
            report.append(f"ITEM unresolved: '{b}' (group '{label}')")
            continue
        apt = len(grp)  # duplicate rows = receipts per decay tier
        counts = grp[0]["counts"]
        item = {"item_name": canon, "points": int(grp[0]["points"])}
        if apt > 1:
            item["awards_per_tier"] = apt
        if not counts:
            item["counts_for_group"] = False
        items.append(item)
    return {"label": label, "npcs": npcs, "bonus_points": int(bonus), "bonus_max": 1,
            "items": items}, npcs


def npc_candidates(setname):
    # split "A / B" into multiple NPCs; strip "(pvm)" etc.
    base = re.sub(r"\s*\(.*?\)\s*", " ", setname).strip()
    return [p.strip() for p in base.split("/") if p.strip()]


# meta tasks
for meta_name, kids in META.items():
    parent = by_name.get(meta_name)
    if not parent:
        continue
    groups = []
    for kid in kids:
        ks = by_name.get(kid)
        if not ks:
            report.append(f"META child missing: '{kid}' under '{meta_name}'")
            continue
        g, _ = build_group(kid, npc_candidates(kid), ks["set_bonus"], ks["items"])
        if g["items"]:
            groups.append(g)
    if groups:
        tasks.append({
            "label": meta_name,
            "config": {"kind": "loot_sweep", "decay_percent": 20, "decay_mode": "linear",
                       "set_bonus_points": int(parent["set_bonus"]), "set_bonus_max": 1,
                       "groups": groups},
        })

# simple (single-group) tasks
for s in sets:
    if s["name"] in META or s["name"] in child_names:
        continue
    if not s["items"]:
        report.append(f"EMPTY set skipped (no items — manual/challenge?): '{s['name']}' bonus={s['set_bonus']}")
        continue
    g, npcs = build_group(s["name"], npc_candidates(s["name"]), s["set_bonus"], s["items"])
    if not g["items"]:
        report.append(f"Set skipped (no resolvable items): '{s['name']}'")
        continue
    if not npcs:
        report.append(f"NPC MISSING for set '{s['name']}' — items will match ANY npc until you set it")
    tasks.append({
        "label": s["name"],
        # simple boss: the group carries the boss bonus, no separate set bonus
        "config": {"kind": "loot_sweep", "decay_percent": 20, "decay_mode": "linear",
                   "set_bonus_points": 0, "set_bonus_max": 1, "groups": [g]},
    })

# Full template payload (ready for an EventTemplate.payload row).
payload = {
    "version": 1,
    "event": {
        "description": "Obtain drops across the game to receive the most points before the event concludes!",
        "formation_mode": "admin_assign", "requires_confirmation": False,
        "submission_policy": "all", "has_bingo": False, "board_size": 5,
        "bonus_line_points": 0, "bonus_blackout_points": 0,
        "mode": "standard", "kind": "loot_sweep",
    },
    "tasks": [
        {"type": "loot_sweep", "label": t["label"], "target": None, "target_value": None,
         "points": 0, "requires_confirmation": False, "visibility": "public",
         "config": t["config"]}
        for t in tasks
    ],
    "teams": [], "bingo": None,
}
json.dump(payload, open(f"{SP}/all_content_template.json", "w"), indent=1)

# Grouped, human-readable review report.
buckets = defaultdict(list)
for line in report:
    key = ("Pets (can't be loot_sweep items — see note)" if line.startswith("PET")
           else "Item names unresolved (need the canonical in-game name)" if line.startswith("ITEM")
           else "NPC names unresolved (fix the source NPC)" if line.startswith("NPC unresolved")
           else "Sets with NO source NPC (match ANY npc until set)" if line.startswith("NPC MISSING")
           else "Empty / skipped sets" if "EMPTY" in line or line.startswith("Set skipped")
           else "Other")
    buckets[key].append(line.split(": ", 1)[-1] if ": " in line else line)
with open(f"{SP}/REVIEW.md", "w") as f:
    f.write("# Loot Sweep (All Content) — generation review\n\n")
    f.write(f"Generated **{len(tasks)}** loot_sweep tasks from the sheet "
            f"(`template_payload.json`). **{len(report)}** things need your eye.\n\n")
    f.write("**Pets note:** loot_sweep items only credit from a *drop* submission "
            "carrying the source NPC. Pets arrive as `pet` submissions, so they "
            "can't be scored as loot_sweep items as-is — every 'pet (bonus)' row "
            "was skipped. Decide separately how you want pet bonuses handled.\n\n")
    for k in sorted(buckets):
        f.write(f"## {k} ({len(buckets[k])})\n\n")
        for v in buckets[k]:
            f.write(f"- {v}\n")
        f.write("\n")

print(f"tasks: {len(tasks)}   review items: {len(report)}")
npc_missing = [t['label'] for t in tasks if any(not g['npcs'] for g in t['config']['groups'])]
print(f"tasks with a group missing NPCs: {len(npc_missing)}")
# category counts of report
cats = defaultdict(int)
for line in report:
    cats[line.split(":")[0].split("(")[0].strip()] += 1
for k, v in sorted(cats.items(), key=lambda x: -x[1]):
    print(f"  {v:>3}  {k}")
