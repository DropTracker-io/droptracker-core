"""Ready-made Conquest maps.

``gielinor`` is the default map: the 45 bosses of the task generator's
curated encounter list (services/task_generator.ENCOUNTERS), each placed in
the part of Gielinor it lives in (checked against the OSRS wiki), laid out as
a schematic map. No Jagex art ships with it: the site draws the regions
itself, and organisers can upload their own map image and move tiles on top.

Every tile gets two rules, built from the encounter catalog the "Fill for me"
generator already assembles (web_api/task_generator_catalog.py):

- **kills**: one troop per N kills, where N is ``troop_hours`` of efficient
  kills at that boss (WOM's EHB rate, then our derived rates). This is what
  keeps the map fair: a troop costs about the same time at Zulrah and at Nex,
  so no team wins by camping the fastest boss.
- **uniques**: ``unique_troops`` troops for any of the boss's Clan Log uniques,
  a lucky drop landing as a surge of troops.

Pure: the caller passes the assembled catalog rows. The route turns the
result into rows through the designer's save path, so a preset map is
editable exactly like a hand-built one.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

#: {preset key: human label} — what the designer offers.
PRESETS = {"gielinor": "Gielinor (45 bosses, 10 regions)"}

#: Region order = display order. ``anchor`` is where the region's cluster of
#: tiles is centred on the schematic map (x east, y south, both 0..1, on a
#: 16:10 canvas); ``color`` tints the region.
GIELINOR_REGIONS: tuple = (
    {"key": "kourend", "name": "Kourend & Kebos", "anchor": (0.10, 0.31),
     "color": "#9b6a3c",
     "tiles": ("chambers_of_xeric", "alchemical_hydra", "sarachnis", "vardorvis", "yama")},
    {"key": "varlamore", "name": "Varlamore", "anchor": (0.14, 0.80),
     "color": "#c9733a",
     "tiles": ("fortis_colosseum", "hueycoatl", "amoxliatl", "moons_of_peril",
               "doom_of_mokhaiotl")},
    {"key": "fremennik", "name": "Fremennik Province", "anchor": (0.30, 0.17),
     "color": "#4f7fa6",
     "tiles": ("dagannoth_kings", "vorkath", "phantom_muspah", "duke_sucellus")},
    {"key": "kandarin", "name": "Kandarin & Tirannwn", "anchor": (0.29, 0.53),
     "color": "#2f8a67",
     "tiles": ("zulrah", "corrupted_gauntlet", "kraken", "thermonuclear_smoke_devil",
               "demonic_gorillas")},
    {"key": "gwd", "name": "God Wars Dungeon", "anchor": (0.47, 0.27),
     "color": "#7a63b0",
     "tiles": ("kree_arra", "general_graardor", "commander_zilyana", "kril_tsutsaroth",
               "nex")},
    {"key": "wilderness", "name": "The Wilderness", "anchor": (0.72, 0.19),
     "color": "#a33a32",
     "tiles": ("callisto_artio", "vetion_calvarion", "venenatis_spindel",
               "chaos_elemental", "wilderness_demi_bosses", "king_black_dragon",
               "corporeal_beast")},
    {"key": "heartlands", "name": "Asgarnia & Misthalin", "anchor": (0.55, 0.61),
     "color": "#5a9a3e",
     "tiles": ("cerberus", "the_whisperer", "royal_titans", "scurrius",
               "tormented_demon")},
    {"key": "abyss", "name": "The Abyss", "anchor": (0.93, 0.18),
     "color": "#4b4fa8",
     "tiles": ("abyssal_sire", "the_leviathan")},
    {"key": "morytania", "name": "Morytania", "anchor": (0.845, 0.52),
     "color": "#6b5a7e",
     "tiles": ("barrows", "theatre_of_blood", "nightmare", "grotesque_guardians",
               "araxxor")},
    {"key": "desert", "name": "Kharidian Desert", "anchor": (0.735, 0.85),
     "color": "#c8a24a",
     "tiles": ("kalphite_queen", "tombs_of_amascut")},
)

#: The schematic canvas (a 16:10 box) the preset positions assume.
CANVAS = (1600, 1000)
#: Centre-to-centre distance between neighbouring tiles in a cluster (px).
TILE_SPACING = 108

#: Troop-cost bounds (hours of one player's efficient play per troop).
TROOP_HOURS_CHOICES = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)
DEFAULT_TROOP_HOURS = 0.5
DEFAULT_UNIQUE_TROOPS = 2
#: How many troops a typical tile should see over the whole event: enough
#: for several captures (defense tops out at 5), few enough that each one
#: still matters.
TARGET_TROOPS_PER_TILE = 20
#: Average hours per member per day spent on event content (the task
#: generator's "normal" activity level).
HOURS_PER_MEMBER_DAY = 1.5

# Axial hex directions in ring-walk order (redblobgames' "hex rings": start
# at radius × (-1, +1), then take ``radius`` steps along each direction).
_RING = ((1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1))
_SQRT3 = math.sqrt(3.0)


def cluster_offsets(n: int) -> list:
    """Pixel offsets for ``n`` tiles packed as a hex cluster: a centre tile,
    then the six around it, then the next ring. Two tiles sit side by side."""
    if n <= 0:
        return []
    if n == 2:
        half = TILE_SPACING / 2.0
        return [(-half, 0.0), (half, 0.0)]
    cells = [(0, 0)]
    radius = 1
    while len(cells) < n:
        q, r = -radius, radius  # start at the south-west corner of the ring
        for dq, dr in _RING:
            for _ in range(radius):
                cells.append((q, r))
                q, r = q + dq, r + dr
        radius += 1
    size = TILE_SPACING / _SQRT3
    out = []
    for q, r in cells[:n]:
        out.append((size * (_SQRT3 * q + _SQRT3 / 2.0 * r), size * 1.5 * r))
    return out


def _nice_hours(hours: float) -> float:
    return min(TROOP_HOURS_CHOICES, key=lambda c: abs(math.log(c) - math.log(hours)))


def suggest_troop_hours(team_count: int, team_size: int, days: float,
                        tile_count: int) -> float:
    """A troop cost that gives each tile about TARGET_TROOPS_PER_TILE troops
    over the event: total member-hours / (tiles × target), snapped to a
    choice. Falls back to the default when the event size is unknown."""
    if team_count <= 0 or team_size <= 0 or days <= 0 or tile_count <= 0:
        return DEFAULT_TROOP_HOURS
    capacity = team_count * team_size * days * HOURS_PER_MEMBER_DAY
    return _nice_hours(max(capacity / (tile_count * TARGET_TROOPS_PER_TILE), 0.01))


def _nice_round(value: float) -> int:
    # Same rounding the task generator puts on tiles ("35", "150", "2,000").
    from services.task_generator import nice_round

    return nice_round(value)


def _kc_rule(row: dict, troop_hours: float) -> dict:
    from services.task_generator import _kc_task

    kills = max(_nice_round(float(row["kph"]) * troop_hours), 1)
    return {"troops": 1, "new_task": _kc_task(row, kills)}


def _unique_rule(row: dict, troops: int) -> Optional[dict]:
    uniques = [u["name"] for u in row.get("uniques") or [] if u.get("name")]
    if not uniques:
        return None
    from services.task_generator import _bare, _item_lock

    return {"troops": troops, "new_task": {
        "type": "item_collection",
        "label": f"Any {_bare(row)} unique",
        "target_value": 1,
        "config": {"kind": "any_of",
                   "items": [{"item_name": u} for u in uniques],
                   "item_npcs": _item_lock(row, uniques)},
    }}


def build_preset_map(preset: str, catalog: Iterable[dict], *,
                     troop_hours: float = DEFAULT_TROOP_HOURS,
                     unique_troops: int = DEFAULT_UNIQUE_TROOPS) -> tuple:
    """``(map_body, skipped)``: a designer-save body (regions + tiles with
    ``new_task`` rules, the shape services.conquest.validate_map takes) and
    the encounter keys left out because the catalog couldn't price them
    (no kill rate / unknown NPC on this database)."""
    if preset != "gielinor":
        raise ValueError(f"Unknown preset: {preset}")
    by_key = {row["key"]: row for row in catalog}
    width, height = CANVAS
    regions, tiles, skipped = [], [], []
    for region in GIELINOR_REGIONS:
        rows = [by_key[k] for k in region["tiles"] if k in by_key]
        skipped += [k for k in region["tiles"] if k not in by_key]
        if not rows:
            continue
        ax, ay = region["anchor"]
        offsets = cluster_offsets(len(rows))
        top = min(dy for _dx, dy in offsets)
        regions.append({
            "key": region["key"], "name": region["name"], "color": region["color"],
            # Risk-style: a bigger region is worth more to hold.
            "bonus": max(math.ceil(len(rows) / 2), 1),
            "label_x": ax, "label_y": max(ay + (top - 58) / height, 0.02),
        })
        for row, (dx, dy) in zip(rows, offsets):
            rules = [_kc_rule(row, troop_hours)]
            unique = _unique_rule(row, unique_troops)
            if unique:
                rules.append(unique)
            npc_ids = row.get("npc_ids") or []
            tiles.append({
                "key": row["key"],
                "label": row.get("short") or row["label"],
                "x": round(min(max(ax + dx / width, 0.02), 0.98), 4),
                "y": round(min(max(ay + dy / height, 0.04), 0.96), 4),
                "kind": "normal", "value": 1, "region_key": region["key"],
                "icon_npc_id": npc_ids[0] if npc_ids else None,
                "rules": rules,
            })
    return {"regions": regions, "tiles": tiles, "preset": preset}, skipped
