"""Draw a Conquest board as SVG from the geometry mapgen.py wrote.

Standard library only: this is the part a live service would run. The
geometry JSON holds the map (coast, biomes, territories, decorations); the
``state`` passed in holds the game (who owns what, defense, teams).

Layers, bottom to top: sea, coast ripples, land and biome patches, terrain
sprites, owned-territory tint, territory borders, region borders, the Abyss
rift, boss medallions, region names, then the frame, title, compass and
legend. No emoji anywhere (the server's Chromium has no emoji font).
"""
from __future__ import annotations

import html
import math
from typing import Callable, Optional

FONT_STACK = "'RSUF', 'RuneScape UF', 'Trebuchet MS', sans-serif"
FRAME = 44
HEADER = 70  # title band above the map

THEMES = {
    "vivid": {
        "sea": ("#3a7fb4", "#1f4f7c"), "shallow": "#8fd0ec", "wave": "#d8f1fb",
        "beach": "#f1dc9c", "coast": "#3a2a17", "ink": "#2b1d0e", "fog": "#eee3c6",
        "biomes": {
            "grass": "#8cb54c", "lowland": "#9fae57", "plains": "#cdb968", "forest": "#4f8d3d",
            "hills": "#a08a5a", "mountain": "#8a6d4c", "rock": "#a8a191", "dark": "#5d534a",
            "desert": "#ebd183", "snow": "#f1f5f7", "swamp": "#566f4c", "bog": "#77845a",
            "wild": "#5e4b43", "ash": "#7d6c5d", "abyss": "#2c2150",
        },
        "terrain_filter": None,
    },
    "parchment": {
        "sea": ("#b9c7c0", "#8fa5a0"), "shallow": "#dfe6d6", "wave": "#6f8580",
        "beach": "#eadcb5", "coast": "#4a3520", "ink": "#3a2814", "fog": "#f3ead3",
        "biomes": {
            "grass": "#d9c9a0", "lowland": "#d4c399", "plains": "#e3d3a8", "forest": "#bfae84",
            "hills": "#cdb98f", "mountain": "#bca780", "rock": "#cfc3a5", "dark": "#a8977a",
            "desert": "#ecdcae", "snow": "#f4eee0", "swamp": "#b3a585", "bog": "#c5b691",
            "wild": "#a99377", "ash": "#b8a88b", "abyss": "#6d5f86",
        },
        "terrain_filter": "sepia",
    },
}

def _label(t: dict) -> str:
    return t.get("label") or t["key"].replace("_", " ").title()


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


# --------------------------------------------------------------------------
# sprites (drawn around a ground point at 0,0; ~30 units tall)

SPRITES = """
<symbol id="s-tree" overflow="visible"><ellipse cx="0" cy="0" rx="8" ry="2.6" fill="#000" opacity=".18"/>
 <rect x="-1.6" y="-7" width="3.2" height="7" rx="1" fill="#6b4423"/>
 <circle cx="0" cy="-13" r="8.2" fill="#3d7a30" stroke="#1d3a17" stroke-width="1.3"/>
 <circle cx="-2.6" cy="-15.6" r="3.4" fill="#5c9d44"/></symbol>
<symbol id="s-tree2" overflow="visible"><ellipse cx="0" cy="0" rx="7" ry="2.4" fill="#000" opacity=".18"/>
 <rect x="-1.4" y="-5" width="2.8" height="5" fill="#6b4423"/>
 <path d="M0 -24 L8 -10 L4 -10 L10 -3 L-10 -3 L-4 -10 L-8 -10Z" fill="#35702d" stroke="#1b3816" stroke-width="1.2" stroke-linejoin="round"/>
 <path d="M0 -24 L-8 -10 L-4 -10 L-10 -3 L-3 -3Z" fill="#4c8c3a"/></symbol>
<symbol id="s-bush" overflow="visible"><ellipse cx="0" cy="0" rx="8" ry="2.2" fill="#000" opacity=".15"/>
 <circle cx="-3.5" cy="-3.5" r="4.4" fill="#4d8a37" stroke="#1d3a17" stroke-width="1.1"/>
 <circle cx="3" cy="-4.2" r="5" fill="#5a9a40" stroke="#1d3a17" stroke-width="1.1"/></symbol>
<symbol id="s-tuft" overflow="visible"><path d="M-5 0 L-6.5 -6 M-1 0 L-1 -8 M3 0 L5 -6.5 M7 0 L8.5 -4" stroke="#7d8a3a" stroke-width="1.6" stroke-linecap="round" fill="none"/></symbol>
<symbol id="s-hill" overflow="visible"><path d="M-18 0 Q-6 -16 6 -9 Q12 -6 18 0Z" fill="#8a744a" stroke="#4d3b20" stroke-width="1.3"/>
 <path d="M-12 -4 Q-5 -12 2 -9" stroke="#b39f70" stroke-width="2" fill="none" stroke-linecap="round"/></symbol>
<symbol id="s-mountain" overflow="visible"><ellipse cx="0" cy="0" rx="18" ry="3" fill="#000" opacity=".18"/>
 <path d="M-18 0 L-4 -26 L2 -19 L7 -24 L19 0Z" fill="#8d7658" stroke="#3b2a17" stroke-width="1.4" stroke-linejoin="round"/>
 <path d="M-4 -26 L1 0 L19 0 L7 -24 L2 -19Z" fill="#6c5840"/></symbol>
<symbol id="s-snowpeak" overflow="visible"><ellipse cx="0" cy="0" rx="18" ry="3" fill="#000" opacity=".15"/>
 <path d="M-18 0 L-4 -28 L2 -21 L7 -25 L19 0Z" fill="#9aa6b2" stroke="#3b4550" stroke-width="1.4" stroke-linejoin="round"/>
 <path d="M-4 -28 L1 0 L19 0 L7 -25 L2 -21Z" fill="#7b8794"/>
 <path d="M-4 -28 L-9 -18 L-5 -16 L-2 -19 L1 -16 L2 -21Z M7 -25 L3 -18 L6 -17 L10 -19Z" fill="#fff"/></symbol>
<symbol id="s-rocks" overflow="visible"><path d="M-9 0 L-7 -6 L-2 -7 L0 0Z" fill="#8f8a80" stroke="#3b3630" stroke-width="1"/>
 <path d="M-1 0 L2 -9 L8 -8 L10 0Z" fill="#a39e93" stroke="#3b3630" stroke-width="1"/></symbol>
<symbol id="s-deadtree" overflow="visible"><ellipse cx="0" cy="0" rx="6" ry="2" fill="#000" opacity=".2"/>
 <path d="M0 0 L0 -14 M0 -9 L-6 -16 M0 -12 L5 -19 M-3 -13 L-5 -18 M3 -16 L7 -16" stroke="#3a2c24" stroke-width="2" stroke-linecap="round" fill="none"/></symbol>
<symbol id="s-lava" overflow="visible"><path d="M-10 -2 L-4 -4 L-1 -1 L5 -5 L10 -2" stroke="#ff7a1a" stroke-width="2.4" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
 <path d="M-10 -2 L-4 -4 L-1 -1 L5 -5 L10 -2" stroke="#ffd35a" stroke-width="0.9" fill="none"/></symbol>
<symbol id="s-dune" overflow="visible"><path d="M-17 0 Q-6 -9 6 -4 Q12 -1 17 0" fill="#e2c173" stroke="#b8924a" stroke-width="1.4"/></symbol>
<symbol id="s-cactus" overflow="visible"><path d="M-1.8 0 V-15 a1.8 1.8 0 0 1 3.6 0 V0Z M-1.8 -7 H-5 V-11 M1.8 -9 H5 V-13" fill="#5c9a45" stroke="#2c5320" stroke-width="1.3" stroke-linejoin="round"/></symbol>
<symbol id="s-pine" overflow="visible"><ellipse cx="0" cy="0" rx="6" ry="2" fill="#000" opacity=".15"/>
 <path d="M0 -24 L7 -12 L4 -12 L9 -4 L-9 -4 L-4 -12 L-7 -12Z" fill="#2f5e45" stroke="#163024" stroke-width="1.2" stroke-linejoin="round"/>
 <path d="M0 -24 L3 -18 L-3 -18Z M-4 -12 L-1 -10 L2 -12 Z" fill="#fff"/><rect x="-1.2" y="-4" width="2.4" height="4" fill="#5b3b22"/></symbol>
<symbol id="s-swamptree" overflow="visible"><ellipse cx="0" cy="0" rx="7" ry="2" fill="#000" opacity=".2"/>
 <path d="M0 0 C-1 -6 2 -9 0 -15 M0 -10 C-4 -12 -7 -12 -9 -16 M0 -13 C4 -15 6 -18 9 -18" stroke="#3b3240" stroke-width="2.2" stroke-linecap="round" fill="none"/>
 <path d="M-8 -15 v5 M8 -17 v6 M-4 -13 v4" stroke="#6f8a5a" stroke-width="1.2" stroke-linecap="round"/></symbol>
<symbol id="s-reeds" overflow="visible"><path d="M-4 0 V-9 M0 0 V-12 M4 0 V-8" stroke="#5f6e3c" stroke-width="1.4" stroke-linecap="round"/>
 <path d="M0 -12 v-3 M-4 -9 v-3" stroke="#6b4a2b" stroke-width="2.4" stroke-linecap="round"/></symbol>
<symbol id="s-wave" overflow="visible"><path d="M-12 0 q3 -4 6 0 t6 0 t6 0 t6 0" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/></symbol>
<symbol id="s-crown" viewBox="0 0 24 18" overflow="visible"><path d="M1 16 L3 4 L8.5 9.5 L12 1 L15.5 9.5 L21 4 L23 16Z" stroke-linejoin="round"/></symbol>
<symbol id="s-swords" overflow="visible"><g stroke="#2b1d0e" stroke-width="1.2" stroke-linejoin="round">
 <path d="M-9 -9 L5 5 L7 3 L-7 -11Z" fill="#e8e8f0"/><path d="M9 -9 L-5 5 L-7 3 L7 -11Z" fill="#e8e8f0"/>
 <path d="M3 7 L7 3 M-3 7 L-7 3" stroke-width="2.6"/></g></symbol>
<symbol id="s-shield" overflow="visible"><path d="M0 -8 L7 -5.5 V0 C7 5 3.5 7.5 0 9 C-3.5 7.5 -7 5 -7 0 V-5.5Z" stroke="#2b1d0e" stroke-width="1.4" stroke-linejoin="round"/></symbol>
"""


def _defs(font_b64: Optional[str], theme: dict) -> str:
    font = (f"@font-face{{font-family:'RSUF';src:url(data:font/ttf;base64,{font_b64}) format('truetype');}}"
            if font_b64 else "")
    sea_a, sea_b = theme["sea"]
    sepia = ('<filter id="sepia"><feColorMatrix type="matrix" values="0.39 0.77 0.19 0 0.02 '
             '0.35 0.69 0.17 0 0.01 0.27 0.53 0.13 0 0 0 0 0 1 0"/></filter>')
    return f"""<defs><style>{font}
.rs{{font-family:{FONT_STACK};}}
.region-name{{font-size:34px;fill:#ff981f;stroke:#000;stroke-width:5px;paint-order:stroke;stroke-linejoin:round;letter-spacing:1px}}
.scroll-text{{font-size:17px;fill:{theme['ink']}}}
</style>
<radialGradient id="sea" cx="50%" cy="45%" r="75%"><stop offset="0" stop-color="{sea_a}"/><stop offset="1" stop-color="{sea_b}"/></radialGradient>
<linearGradient id="gold" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fbe39a"/><stop offset=".5" stop-color="#d9a441"/><stop offset="1" stop-color="#8f5f1d"/></linearGradient>
<radialGradient id="rift" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#b58cff"/><stop offset=".35" stop-color="#5a3aa8"/><stop offset="1" stop-color="#1a1236"/></radialGradient>
<filter id="paper" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" baseFrequency=".035" numOctaves="3" seed="4"/>
 <feColorMatrix values="0 0 0 0 .2 0 0 0 0 .15 0 0 0 0 .05 0 0 0 .55 -.18"/></filter>
<filter id="drop" x="-30%" y="-30%" width="160%" height="170%"><feDropShadow dx="0" dy="2.5" stdDeviation="2" flood-color="#000" flood-opacity=".45"/></filter>
<pattern id="hatch" width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(40)"><line x1="0" y1="0" x2="0" y2="12" stroke="#5b4a2e" stroke-width="1.6" opacity=".28"/></pattern>
<filter id="soft"><feGaussianBlur stdDeviation="6"/></filter>
{sepia}
{SPRITES}
</defs>"""


# --------------------------------------------------------------------------

def render_board(geom: dict, state: dict, *, theme: str = "vivid",
                 portrait: Callable[[str], Optional[str]] = lambda key: None,
                 font_b64: Optional[str] = None) -> str:
    th = THEMES[theme]
    W, H = geom["width"], geom["height"]
    teams = {t["id"]: t for t in state.get("teams", [])}
    tstate = state.get("tiles", {})
    owner = {k: v.get("owner") for k, v in tstate.items()}
    region_tiles = {}
    for t in geom["tiles"]:
        region_tiles.setdefault(t["region"], []).append(t["key"])
    region_owner = {}
    for rk, keys in region_tiles.items():
        owners = {owner.get(k) for k in keys}
        if len(owners) == 1 and None not in owners:
            region_owner[rk] = owners.pop()

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           f'viewBox="{-FRAME} {-FRAME - HEADER} {W + 2 * FRAME} {H + 2 * FRAME + HEADER}" '
           f'width="{W + 2 * FRAME}" height="{H + 2 * FRAME + HEADER}">']
    out.append(_defs(font_b64, th))
    out.append(f'<clipPath id="landclip"><path d="{geom["land"]}" clip-rule="evenodd"/></clipPath>')
    out.append(f'<clipPath id="boardclip"><rect x="0" y="0" width="{W}" height="{H}"/></clipPath>')

    # ---- terrain -------------------------------------------------------
    tf = f' filter="url(#{th["terrain_filter"]})"' if th["terrain_filter"] else ""
    out.append('<g clip-path="url(#boardclip)">')
    out.append(f'<g{tf}>')
    out.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="url(#sea)"/>')
    for w_, op in ((46, .12), (30, .2), (16, .32)):
        out.append(f'<path d="{geom["land"]}" fill="none" stroke="{th["shallow"]}" stroke-width="{w_}" '
                   f'stroke-opacity="{op}" stroke-linejoin="round"/>')
    for x, y in geom["waves"]:
        out.append(f'<use href="#s-wave" x="{x}" y="{y}" color="{th["wave"]}" opacity=".45"/>')
    out.append(f'<path d="{geom["land"]}" fill="{th["beach"]}" fill-rule="evenodd"/>')
    out.append('<g clip-path="url(#landclip)">')
    order = ("grass", "lowland", "plains", "desert", "forest", "hills", "mountain", "rock", "dark",
             "snow", "bog", "swamp", "ash", "wild", "abyss")
    for name in order:
        d = geom["biomes"].get(name)
        if d:
            out.append(f'<path d="{d}" fill="{th["biomes"][name]}" fill-rule="evenodd"/>')
    # a sandy lip just inside every coastline
    out.append(f'<path d="{geom["land"]}" fill="none" stroke="{th["beach"]}" stroke-width="9" opacity=".9"/>')
    out.append(f'<rect x="0" y="0" width="{W}" height="{H}" filter="url(#paper)" opacity=".5"/>')
    out.append('</g>')
    out.append(f'<path d="{geom["land"]}" fill="none" stroke="{th["coast"]}" stroke-width="3.2" '
               f'stroke-linejoin="round"/>')
    for kind, x, y, s in geom["decor"]:
        out.append(f'<use href="#s-{kind}" transform="translate({x} {y}) scale({s})"/>')
    # Land outside every region is scenery: washed out and hatched so the
    # kingdoms in play stand out.
    if geom.get("neutral"):
        out.append(f'<g clip-path="url(#landclip)"><path d="{geom["neutral"]}" fill="{th["fog"]}" '
                   f'fill-opacity=".5" fill-rule="evenodd"/>'
                   f'<path d="{geom["neutral"]}" fill="url(#hatch)" fill-rule="evenodd"/></g>')
    out.append('</g>')  # terrain filter

    # ---- the Abyss rift -------------------------------------------------
    ab = geom["abyss"]
    tx, ty = ab["tether"]
    out.append(f'<path d="M{tx} {ty} Q{(tx + ab["x"]) / 2} {min(ty, ab["y"]) - 260} {ab["x"] - ab["r"] * .7} '
               f'{ab["y"] + ab["r"] * .7}" fill="none" stroke="#c9b3ff" stroke-width="3" '
               f'stroke-dasharray="2 9" stroke-linecap="round" opacity=".9"/>')
    out.append(f'<circle cx="{tx}" cy="{ty}" r="9" fill="url(#rift)" stroke="#1a1236" stroke-width="2"/>')
    out.append(f'<circle cx="{ab["x"]}" cy="{ab["y"]}" r="{ab["r"] + 16}" fill="#6b4fd8" opacity=".35" filter="url(#soft)"/>')
    out.append(f'<circle cx="{ab["x"]}" cy="{ab["y"]}" r="{ab["r"]}" fill="url(#rift)"/>')
    spiral = []
    for k in range(3):
        pts = []
        for i in range(60):
            a = i / 59 * math.pi * 2.2 + k * 2.1
            rr = ab["r"] * (0.12 + 0.85 * i / 59)
            pts.append(f'{ab["x"] + rr * math.cos(a):.1f} {ab["y"] + rr * math.sin(a):.1f}')
        spiral.append("M" + " L".join(pts))
    out.append(f'<path d="{"".join(spiral)}" fill="none" stroke="#d9c7ff" stroke-width="2" opacity=".35"/>')

    # ---- territories ----------------------------------------------------
    out.append('<g clip-path="url(#landclip)">')
    for t in geom["tiles"]:
        tid = owner.get(t["key"])
        if tid is None or tid not in teams:
            continue
        col = teams[tid]["color"]
        cid = f'c-{t["key"]}'
        out.append(f'<clipPath id="{cid}"><path d="{t["path"]}"/></clipPath>')
        out.append(f'<path d="{t["path"]}" fill="{col}" fill-opacity=".5"/>')
        out.append(f'<path d="{t["path"]}" fill="none" stroke="{col}" stroke-width="12" '
                   f'stroke-opacity=".85" clip-path="url(#{cid})"/>')
    for t in geom["tiles"]:
        out.append(f'<path d="{t["path"]}" fill="none" stroke="{th["ink"]}" stroke-width="2" '
                   f'stroke-dasharray="7 5" stroke-opacity=".55"/>')
    for r in geom["regions"]:
        out.append(f'<path d="{r["path"]}" fill="none" stroke="{th["ink"]}" stroke-width="6" '
                   f'stroke-opacity=".75" stroke-linejoin="round"/>')
        out.append(f'<path d="{r["path"]}" fill="none" stroke="#f6e7b8" stroke-width="1.6" '
                   f'stroke-opacity=".9" stroke-linejoin="round"/>')
    out.append('</g>')

    # ---- sea lanes for territories with no land neighbour ---------------
    linked = {k for e in geom["edges"] for k in e}
    anchors = {t["key"]: (t["x"], t["y"]) for t in geom["tiles"]}
    for t in geom["tiles"]:
        if t["key"] in linked or t["region"] == "abyss":
            continue
        x, y = anchors[t["key"]]
        near = min((k for k in anchors if k != t["key"] and geom_region(geom, k) != "abyss"),
                   key=lambda k: math.hypot(anchors[k][0] - x, anchors[k][1] - y))
        nx, ny = anchors[near]
        mx, my = (x + nx) / 2 + (ny - y) * .18, (y + ny) / 2 - (nx - x) * .18
        out.append(f'<path d="M{x} {y} Q{mx:.1f} {my:.1f} {nx} {ny}" fill="none" stroke="#fff6d8" '
                   f'stroke-width="3" stroke-dasharray="1 9" stroke-linecap="round" opacity=".85"/>')

    # ---- medallions -----------------------------------------------------
    for t in sorted(geom["tiles"], key=lambda t: t["y"]):
        out.append(_medallion(t, tstate.get(t["key"], {}), teams, th, portrait(t["key"])))

    # ---- region names ---------------------------------------------------
    for r in geom["regions"]:
        if r["key"] == "abyss":
            x, y = ab["x"], ab["y"] - ab["r"] - 22
        elif r["label"]:
            x, y = r["label"]
        else:
            continue
        name = _esc(r["name"])
        rk = region_owner.get(r["key"])
        if rk is not None and rk in teams:
            col = teams[rk]["color"]
            out.append(f'<use href="#s-crown" x="{x - 21}" y="{y - 64}" width="42" height="32" '
                       f'fill="{col}" stroke="#000" stroke-width="2.4" filter="url(#drop)"/>')
            out.append(f'<text x="{x}" y="{y}" text-anchor="middle" class="rs region-name" '
                       f'style="fill:{col}">{name}</text>')
        else:
            out.append(f'<text x="{x}" y="{y}" text-anchor="middle" class="rs region-name">{name}</text>')
    out.append('</g>')  # boardclip

    out.append(_frame(W, H, th))
    out.append(_title(state, W, H))
    out.append(_compass(W - 120, H - 120))
    out.append(_legend(state, geom, owner, region_owner, teams))
    out.append('</svg>')
    return "\n".join(out)


def geom_region(geom, key):
    for t in geom["tiles"]:
        if t["key"] == key:
            return t["region"]
    return None


def _medallion(t, st, teams, th, img_uri) -> str:
    x, y = t["x"], t["y"]
    tid = st.get("owner")
    team = teams.get(tid) if tid is not None else None
    ring = team["color"] if team else "url(#gold)"
    out = [f'<g transform="translate({x} {y})">']
    out.append('<g filter="url(#drop)">')
    out.append(f'<circle r="30" fill="{ring}" stroke="{th["ink"]}" stroke-width="2.6"/>')
    if team:  # a thin gold bevel inside a team ring keeps it looking like a token
        out.append('<circle r="25.8" fill="none" stroke="#fbe39a" stroke-width="1.4" opacity=".9"/>')
    out.append(f'<circle r="23.5" fill="#f3e6c4" stroke="{th["ink"]}" stroke-width="1.6"/>')
    out.append('</g>')
    if img_uri:
        cid = f'p-{t["key"]}'
        out.append(f'<clipPath id="{cid}"><circle r="22.5"/></clipPath>')
        out.append(f'<image href="{img_uri}" x="-22.5" y="-22.5" width="45" height="45" '
                   f'clip-path="url(#{cid})" preserveAspectRatio="xMidYMid meet"/>')
    if st.get("flash"):
        out.append('<circle r="35" fill="none" stroke="#ff3b2f" stroke-width="3" stroke-dasharray="5 4"/>')
        out.append('<use href="#s-swords" x="-26" y="-26"/>')
    d = int(st.get("defense") or 0)
    if team and d:
        out.append(f'<g transform="translate(23 19)"><use href="#s-shield" fill="{team["color"]}" '
                   f'transform="scale(1.45)"/><text y="4.6" text-anchor="middle" class="rs" '
                   f'style="font-size:14px;fill:#fff;stroke:#000;stroke-width:3px;paint-order:stroke">{d}</text></g>')
    label = _esc(_label(t))
    wd = max(len(label) * 8.4 + 18, 44)
    out.append(f'<g transform="translate(0 45)">'
               f'<path d="M{-wd / 2 - 8} -9 L{-wd / 2} -9 L{-wd / 2} 9 L{-wd / 2 - 8} 9 L{-wd / 2 - 3} 0Z '
               f'M{wd / 2 + 8} -9 L{wd / 2} -9 L{wd / 2} 9 L{wd / 2 + 8} 9 L{wd / 2 + 3} 0Z" '
               f'fill="#d8c393" stroke="{th["ink"]}" stroke-width="1.3"/>'
               f'<rect x="{-wd / 2}" y="-11" width="{wd}" height="22" rx="3" fill="#f3e6c4" '
               f'stroke="{th["ink"]}" stroke-width="1.5"/>'
               f'<text y="5.5" text-anchor="middle" class="rs scroll-text">{label}</text></g>')
    out.append('</g>')
    return "".join(out)


def _frame(W, H, th) -> str:
    f, hb = FRAME, HEADER
    top = -f - hb
    return (f'<g><path d="M{-f} {top} H{W + f} V{H + f} H{-f}Z M0 0 V{H} H{W} V0Z" fill="#3e3529" fill-rule="evenodd"/>'
            f'<rect x="{-f + 5}" y="{top + 5}" width="{W + 2 * f - 10}" height="{H + 2 * f + hb - 10}" fill="none" stroke="#5c4f3a" stroke-width="3"/>'
            f'<rect x="-9" y="-9" width="{W + 18}" height="{H + 18}" fill="none" stroke="#1d1810" stroke-width="5"/>'
            f'<rect x="-3" y="-3" width="{W + 6}" height="{H + 6}" fill="none" stroke="#c8a24a" stroke-width="3"/>'
            + "".join(f'<circle cx="{cx}" cy="{cy}" r="9" fill="url(#gold)" stroke="#1d1810" stroke-width="2"/>'
                      for cx, cy in ((-f / 2, top + f / 2), (W + f / 2, top + f / 2), (-f / 2, H + f / 2), (W + f / 2, H + f / 2)))
            + '</g>')


def _title(state, W, H) -> str:
    title = _esc(state.get("title") or "Conquest")
    sub = _esc(state.get("subtitle") or "")
    y = -HEADER / 2 - 4
    return (f'<text x="8" y="{y + 14}" class="rs" style="font-size:44px;fill:#ff981f;stroke:#000;stroke-width:5px;paint-order:stroke">{title}</text>'
            f'<text x="{W - 8}" y="{y + 10}" text-anchor="end" class="rs" style="font-size:24px;fill:#ffff00;stroke:#000;stroke-width:4px;paint-order:stroke">{sub}</text>')


def _compass(cx, cy) -> str:
    pts = []
    for i, (ang, ln, col) in enumerate(((0, 62, "#c9302c"), (90, 50, "#efe2bf"), (180, 50, "#efe2bf"), (270, 50, "#efe2bf"))):
        a = math.radians(ang - 90)
        tipx, tipy = cx + ln * math.cos(a), cy + ln * math.sin(a)
        lx, ly = cx + 11 * math.cos(a - math.pi / 2), cy + 11 * math.sin(a - math.pi / 2)
        rx, ry = cx + 11 * math.cos(a + math.pi / 2), cy + 11 * math.sin(a + math.pi / 2)
        pts.append(f'<path d="M{lx:.1f} {ly:.1f} L{tipx:.1f} {tipy:.1f} L{cx} {cy}Z" fill="{col}" stroke="#2b1d0e" stroke-width="1.6"/>'
                   f'<path d="M{rx:.1f} {ry:.1f} L{tipx:.1f} {tipy:.1f} L{cx} {cy}Z" fill="#8a6d3b" stroke="#2b1d0e" stroke-width="1.6"/>')
    return (f'<g filter="url(#drop)"><circle cx="{cx}" cy="{cy}" r="40" fill="#efe2bf" stroke="#2b1d0e" stroke-width="2.5"/>'
            f'<circle cx="{cx}" cy="{cy}" r="33" fill="none" stroke="#b69457" stroke-width="2"/>'
            + "".join(pts) +
            f'<circle cx="{cx}" cy="{cy}" r="6" fill="url(#gold)" stroke="#2b1d0e" stroke-width="1.5"/>'
            f'<text x="{cx}" y="{cy - 68}" text-anchor="middle" class="rs" style="font-size:22px;fill:#ff981f;stroke:#000;stroke-width:4px;paint-order:stroke">N</text></g>')


def _legend(state, geom, owner, region_owner, teams) -> str:
    if not teams:
        return ""
    rows = []
    for t in state["teams"]:
        held = sum(1 for k, v in owner.items() if v == t["id"])
        regs = sum(1 for v in region_owner.values() if v == t["id"])
        rows.append((t, held, regs))
    rows.sort(key=lambda r: (-(r[0].get("score") or 0), -r[1]))
    x, y = 30, geom["height"] - 30 - 44 - len(rows) * 34
    w, h = 430, 44 + len(rows) * 34 + 14
    out = [f'<g transform="translate({x} {y})" filter="url(#drop)">'
           f'<rect width="{w}" height="{h}" rx="6" fill="#3e3529" stroke="#c8a24a" stroke-width="3" opacity=".96"/>'
           f'<text x="16" y="30" class="rs" style="font-size:22px;fill:#ff981f;stroke:#000;stroke-width:3px;paint-order:stroke">Standings</text>'
           f'<text x="{w - 196}" y="30" text-anchor="middle" class="rs" style="font-size:15px;fill:#c8b78f">Tiles</text>'
           f'<text x="{w - 118}" y="30" text-anchor="middle" class="rs" style="font-size:15px;fill:#c8b78f">Regions</text>'
           f'<text x="{w - 16}" y="30" text-anchor="end" class="rs" style="font-size:15px;fill:#c8b78f">Points</text>']
    for i, (t, held, regs) in enumerate(rows):
        yy = 44 + i * 34
        out.append(f'<g transform="translate(16 {yy})"><rect width="22" height="22" rx="4" fill="{t["color"]}" stroke="#000" stroke-width="1.5"/>'
                   f'<text x="34" y="17" class="rs" style="font-size:19px;fill:#fff;stroke:#000;stroke-width:3px;paint-order:stroke">{_esc(t["name"])}</text>'
                   f'<text x="{w - 196}" y="17" text-anchor="middle" class="rs" style="font-size:19px;fill:#ffff00">{held}</text>'
                   f'<text x="{w - 118}" y="17" text-anchor="middle" class="rs" style="font-size:19px;fill:#ffff00">{regs}</text>'
                   f'<text x="{w - 32}" y="17" text-anchor="end" class="rs" style="font-size:19px;fill:#ffff00">{t.get("score", 0):,}</text></g>')
    out.append('</g>')
    return "".join(out)
