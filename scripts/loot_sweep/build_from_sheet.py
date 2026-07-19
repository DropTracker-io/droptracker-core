"""Generate the 'Loot Sweep (All Content)' template config from the sheet.

Reads gviz.csv, resolves item + NPC names against the DB, folds meta-sets
(Barrows/DKs/Moons) into one task with groups, collapses the sheet's duplicate
"(1,3,5,7,9)" rows into one item with awards_per_tier, and writes:
  - loot_sweep_template.json  (the tasks, ready for a template payload)
  - report.txt                (everything skipped / needing human review)

Does NOT touch prod. Pets are skipped (they arrive as `pet` submissions, not
NPC-scoped drops, so loot_sweep can't credit them) and reported.
"""
import csv, json, os, re, sys
from collections import defaultdict
from dotenv import load_dotenv
import pymysql

SP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(SP, "..", "..")))  # repo root -> utils.*
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
NAME_BY_ID, ID_BY_NORM = {}, {}
for i, n in cur.fetchall():
    NAME_BY_ID[int(i)] = n
    ID_BY_NORM.setdefault(" ".join(n.strip().lower().split()), int(i))
conn.close()


def norm(s):
    return " ".join(str(s or "").strip().lower().split())


def resolve_item(name):
    return ITEM_BY_NORM.get(norm(name))


def resolve_item_with_id(name):
    canon = resolve_item(name)
    return canon, (ID_BY_NORM.get(norm(canon)) if canon else None)


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


from utils.osrs_pets import canonical_pet_name

# Boss/sub-boss header -> its pet's taxonomy name. Pets score from `pet`
# submissions (source:"pet"), matched by name — a pet only comes from its boss.
PET_BY_BOSS = {
    "kree'arra": "Pet kree'arra", "general graardor": "Pet general graardor",
    "commander zilyana": "Pet zilyana", "k'ril tsutsaroth": "Pet k'ril tsutsaroth",
    "nex": "Nexling", "chaos elemental": "Pet chaos elemental",
    "venenatis / spindel": "Venenatis spiderling", "callisto / artio": "Callisto cub",
    "vet'ion / calvar'ion": "Vet'ion jr.", "king black dragon": "Prince black dragon",
    "sarachnis": "Sraracha", "dagannoth supreme": "Pet dagannoth supreme",
    "dagannoth rex": "Pet dagannoth rex", "dagannoth prime": "Pet dagannoth prime",
    "zulrah": "Pet snakeling", "vorkath": "Vorki", "vardorvis": "Butch",
    "duke sucellus": "Baron", "the whisperer": "Wisp", "the leviathan": "Lil'viathan",
    "phantom muspah": "Muphin", "kalphite queen": "Kalphite princess",
    "hueycoatl": "Huberte", "nightmare": "Little nightmare", "amoxliatl": "Moxi",
    "grotesque guardians": "Noon", "kraken (boss)": "Pet kraken", "cerberus": "Hellpuppy",
    "araxxor": "Nid", "thermonuclear devil": "Pet smoke devil",
    "alchemical hydra": "Ikkle hydra", "gauntlet": "Youngllef", "scurrius": "Scurry",
    "chambers of xeric": "Olmlet", "theater of blood": "Lil' zik",
    "tombs of amascut": "Tumeken's guardian",
}

# Shorthand / (bonus)-stripped item name -> canonical DB name (user-provided
# fixes for rows the id-bridge can't resolve).
ITEM_ALIAS = {
    "vorkath head": "Vorkath's head", "kbd heads": "Kbd heads",
    "pendant of ates": "Pendant of ates", "kit": "Twisted ancestral colour kit",
    "dust": "Metamorphic dust", "sanguine dust": "Sanguine dust",
    "jar of venom": "Jar of venom", "tanzanite mutagen": "Tanzanite mutagen",
    "magma mutagen": "Magma mutagen",
}

# Multi-source section sets -> source NPC list. Empty [] means "match any NPC",
# which is safe when the items are unique to those sources anyway.
REVENANTS = sorted(v for v in NPC_BY_NORM.values() if v.lower().startswith("revenant "))
NPC_SET = {
    "theater of blood": [n for n in ["Theatre of Blood"] if resolve_npc(n)],
    "wilderness wards": [n for n in ["Chaos Fanatic", "Crazy archaeologist", "Scorpia"]
                         if resolve_npc(n)],
    "revenant caves (pvm)": REVENANTS,
    "virtus set": [n for n in ["Vardorvis", "Duke Sucellus", "The Whisperer", "The Leviathan"]
                   if resolve_npc(n)],
}


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
    # strip a trailing "(1, 3, 5, 7, 9)" tier suffix and a "(bonus)" marker
    n = re.sub(r"\s*\(\s*\d+(?:\s*,\s*\d+)*\s*\)\s*$", "", n)
    n = re.sub(r"\s*\(bonus\)\s*$", "", n, flags=re.I)
    return n.strip()


def resolve_item_named(b, iid):
    """Resolve a de-suffixed item base name -> canonical DB name. Tries a
    user-provided alias, then the DB name, then the sheet id, then a
    '<x> champion scroll' variant (Champion Scrolls section)."""
    if norm(b) in ITEM_ALIAS and resolve_item(ITEM_ALIAS[norm(b)]):
        return resolve_item(ITEM_ALIAS[norm(b)])
    c = resolve_item(b) or resolve_item(b.rstrip("s"))
    if c:
        return c
    if b.lower().endswith(" scroll"):  # "goblin scroll" -> "Goblin champion scroll"
        c = resolve_item(b[:-7] + " champion scroll")
        if c:
            return c
    if iid is not None:
        return NAME_BY_ID.get(iid)
    return None


def build_group(label, npc_names, bonus, raw_items):
    """One group from a set's raw item rows: resolve drop items + pets, collapse
    batched duplicate rows (awards_per_tier), skip+report unknowns."""
    npcs = []
    for nn in npc_names:
        c = resolve_npc(nn)
        if c and c not in npcs:
            npcs.append(c)
        elif not c:
            report.append(f"NPC unresolved: '{nn}' (group '{label}')")

    items = []
    # pets: one per set (source:"pet", matched by name; don't gate the bonus)
    for it in raw_items:
        if not it["is_pet"]:
            continue
        pet = PET_BY_BOSS.get(norm(label))
        canon = canonical_pet_name(pet) if pet else None
        if not canon:
            report.append(f"PET unresolved for '{label}' ({it['points']}pts) — add the pet name")
            continue
        _, pid = resolve_item_with_id(canon)
        item = {"item_name": canon, "points": int(it["points"]),
                "source": "pet", "counts_for_group": False}
        if pid:
            item["item_id"] = pid
        items.append(item)

    # drop items: collapse duplicate rows (batched decay)
    batches, order = defaultdict(list), []
    for it in raw_items:
        if it["is_pet"]:
            continue
        b = base_item_name(it["name"])
        # ToB "ornament kit" is two items (holy + sanguine)
        keys = (["Holy ornament kit", "Sanguine ornament kit"]
                if norm(b) == "ornament kit" else [b])
        for k in keys:
            if k not in batches:
                order.append(k)
            batches[k].append(it)
    for b in order:
        grp = batches[b]
        canon = resolve_item_named(b, grp[0].get("id"))
        if not canon:
            report.append(f"ITEM unresolved: '{b}' (group '{label}')")
            continue
        apt = len(grp)
        item = {"item_name": canon, "points": int(grp[0]["points"])}
        if apt > 1:
            item["awards_per_tier"] = apt
        if not grp[0]["counts"]:
            item["counts_for_group"] = False
        items.append(item)
    return {"label": label, "npcs": npcs, "bonus_points": int(bonus), "bonus_max": 1,
            "items": items}, npcs


def npc_candidates(setname):
    # multi-source section sets carry an explicit NPC list; otherwise split
    # "A / B" into multiple NPCs and strip "(pvm)" etc.
    if norm(setname) in NPC_SET:
        return list(NPC_SET[norm(setname)])
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
