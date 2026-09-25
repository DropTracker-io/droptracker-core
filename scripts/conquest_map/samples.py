"""Render sample Conquest boards: a fresh map, a mid-game map, and the
mid-game map in the parchment theme, as SVG and PNG (via the box's headless
Chromium, the same one that screenshots Discord board images).

    python scripts/conquest_map/samples.py gielinor.json out/

Portraits come from static/assets/img/npcdb and the title font from the web
repo (RuneScape UF); both are optional.
"""
from __future__ import annotations

import base64
import io
import json
import os
import random
import subprocess
import sys

from PIL import Image

from render import render_board

HERE = os.path.dirname(os.path.abspath(__file__))
# static/assets/img is not in git, so a fresh checkout falls back to the live tree.
NPCDB = next((p for p in (os.path.normpath(os.path.join(HERE, "..", "..", "static", "assets", "img", "npcdb")),
                          "/store/droptracker/disc/static/assets/img/npcdb") if os.path.isdir(p)), "")
FONT = "/store/droptracker/web/apps/web/app/fonts/runescape_uf.ttf"
# Portrait per tile: the npc_list id the site already shows for each
# encounter (services/task_generator.ENCOUNTERS, looked up by name).
NPC_IDS = {
    "chambers_of_xeric": 13696, "theatre_of_blood": 13699, "tombs_of_amascut": 13695,
    "kree_arra": 3162, "general_graardor": 2215, "commander_zilyana": 2205,
    "kril_tsutsaroth": 3129, "nex": 11278, "vardorvis": 12223, "duke_sucellus": 12191,
    "the_whisperer": 12204, "the_leviathan": 12214, "zulrah": 2042, "vorkath": 8060,
    "phantom_muspah": 12077, "kalphite_queen": 963, "hueycoatl": 13949, "nightmare": 9425,
    "amoxliatl": 13977, "corrupted_gauntlet": 13942, "scurrius": 7221,
    "fortis_colosseum": 13741, "tormented_demon": 13599, "corporeal_beast": 319, "yama": 14176,
    "royal_titans": 13980, "doom_of_mokhaiotl": 14707, "sarachnis": 8713,
    "grotesque_guardians": 13960, "abyssal_sire": 5886, "kraken": 494, "cerberus": 5862,
    "araxxor": 13668, "thermonuclear_smoke_devil": 499, "alchemical_hydra": 8615,
    "demonic_gorillas": 7144, "chaos_elemental": 2054, "venenatis_spindel": 6504,
    "callisto_artio": 6503, "vetion_calvarion": 6611, "king_black_dragon": 239,
    "wilderness_demi_bosses": 6619, "barrows": 13729, "dagannoth_kings": 2267,
    "moons_of_peril": 13740,
}

TEAMS = [
    {"id": 1, "name": "Red Dragons", "color": "#d8443a"},
    {"id": 2, "name": "Blue Moons", "color": "#3f7fd9"},
    {"id": 3, "name": "Green Guthix", "color": "#3aa65a"},
    {"id": 4, "name": "Gold Crowns", "color": "#e6b82e"},
]

_cache: dict = {}


def portrait(key: str):
    if key in _cache:
        return _cache[key]
    nid = NPC_IDS.get(key)
    path = f"{NPCDB}/{nid}.png"
    uri = None
    if nid and os.path.exists(path):
        im = Image.open(path).convert("RGBA")
        im.thumbnail((96, 96), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    _cache[key] = uri
    return uri


def midgame_state(geom: dict, seed: int = 5) -> dict:
    rng = random.Random(seed)
    owner = {}
    home = {1: ("wilderness", "gwd"), 2: ("morytania", "desert", "abyss"),
            3: ("kandarin", "fremennik"), 4: ("kourend", "varlamore", "heartlands")}
    tiles = {t["key"]: t for t in geom["tiles"]}
    for tid, regions in home.items():
        for t in geom["tiles"]:
            if t["region"] in regions:
                owner[t["key"]] = tid
    # Break up the tidy start: some tiles still neutral, some stolen.
    keys = sorted(owner)
    for k in rng.sample(keys, 7):
        owner[k] = None
    for k in rng.sample(keys, 7):
        if tiles[k]["region"] != "wilderness":
            owner[k] = rng.choice([1, 2, 3, 4])
    state_tiles = {}
    for k, v in owner.items():
        state_tiles[k] = {"owner": v, "defense": rng.randint(1, 5) if v else 0}
    for k in ("vetion_calvarion", "scurrius", "zulrah"):
        state_tiles[k]["flash"] = True
    held = {t["id"]: 0 for t in TEAMS}
    for v in owner.values():
        if v:
            held[v] += 1
    teams = [dict(t, score=held[t["id"]] * 38 + rng.randint(0, 60)) for t in TEAMS]
    teams[0]["score"] += 120  # Red holds a whole region
    return {"title": "Conquest of Gielinor", "subtitle": "Day 6 of 14   |   45 territories   |   10 regions",
            "teams": teams, "tiles": state_tiles}


def fresh_state() -> dict:
    return {"title": "Conquest of Gielinor", "subtitle": "Day 1 of 14   |   45 territories   |   10 regions",
            "teams": [], "tiles": {}}


def rasterize(svg: str, out_png: str, width: int, height: int) -> None:
    html_path = os.path.abspath(out_png[:-4] + ".html")
    out_png = os.path.abspath(out_png)
    with open(html_path, "w") as fh:
        fh.write('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;'
                 'background:#1d1810}svg{display:block}</style></head><body>' + svg + "</body></html>")
    subprocess.run(["nice", "/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                    "--hide-scrollbars", "--force-device-scale-factor=1",
                    f"--window-size={width},{height}", "--virtual-time-budget=4000",
                    f"--screenshot={out_png}", "file://" + html_path],
                   check=True, capture_output=True, timeout=180)


def main(geom_path: str, out_dir: str, only: str = "") -> None:
    geom = json.load(open(geom_path))
    font_b64 = (base64.b64encode(open(FONT, "rb").read()).decode()
                if os.path.exists(FONT) else None)
    from render import FRAME, HEADER
    w, h = geom["width"] + 2 * FRAME, geom["height"] + 2 * FRAME + HEADER
    jobs = {
        "board_fresh": (fresh_state(), "vivid"),
        "board_midgame": (midgame_state(geom), "vivid"),
        "board_parchment": (midgame_state(geom), "parchment"),
    }
    for name, (state, theme) in jobs.items():
        if only and only not in name:
            continue
        svg = render_board(geom, state, theme=theme, portrait=portrait, font_b64=font_b64)
        with open(os.path.join(out_dir, name + ".svg"), "w") as fh:
            fh.write(svg)
        png = os.path.join(out_dir, name + ".png")
        rasterize(svg, png, w, h)
        print(name, png, f"{len(svg) // 1024} KiB svg")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
