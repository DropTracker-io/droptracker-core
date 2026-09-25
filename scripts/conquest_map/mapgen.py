"""Build a Conquest board's geometry from the real OSRS world map.

Offline step (numpy + scipy + scikit-image, none of which the live services
need): reads the wiki's world map, works out land and water, sorts the land
into a handful of cartoon biomes by its real colours, carves one territory
per boss around its real location, and writes everything as smooth SVG
paths in one JSON file. The renderer (render.py) only reads that JSON, so
drawing a board at runtime needs nothing beyond the standard library.

    python scripts/conquest_map/mapgen.py world.png gielinor.json

world.png is the wiki's full world map, File:Old School RuneScape world
map.png (https://oldschool.runescape.wiki/images/Old_School_RuneScape_world_map.png,
9216 x 6528). Needs numpy, scipy, scikit-image and Pillow; the production
venv has only numpy and Pillow, so install the rest somewhere private
(pip install --target /tmp/pylib scipy scikit-image and PYTHONPATH).

Board units are game tiles: bx = x - x0, by = y1 - y (y flipped so north is
up), so the board is (x1 - x0) x (y1 - y0) units.
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage as ndi
from skimage import measure

import geo

Image.MAX_IMAGE_PIXELS = None

G = 2  # game tiles per working pixel

# Cluster centres of the wiki map's land colours (k-means on the blurred
# map), each mapped to the cartoon biome it reads as.
CENTRES = (
    ((87, 97, 24), "grass"), ((94, 93, 64), "lowland"), ((72, 66, 48), "hills"),
    ((66, 55, 22), "mountain"), ((146, 129, 53), "plains"), ((103, 112, 107), "rock"),
    ((39, 39, 33), "dark"), ((184, 165, 91), "desert"), ((46, 88, 46), "forest"),
    ((144, 144, 128), "rock"), ((220, 223, 222), "snow"), ((180, 183, 182), "snow"),
)
BIOMES = ("grass", "lowland", "plains", "forest", "hills", "mountain", "rock", "dark",
          "desert", "snow", "swamp", "bog", "wild", "ash", "abyss")

# Kingdom-flavoured overrides (game-coordinate boxes): Morytania's greens are
# swamp, the Wilderness's browns are ash and scorched earth.
OVERRIDES = (
    ((3400, 3960, 3140, 3600), {"forest": "swamp", "grass": "bog", "lowland": "bog",
                                "plains": "bog", "hills": "swamp"}),
    ((2940, 3400, 3525, 3975), {"dark": "wild", "hills": "ash", "mountain": "wild",
                                "lowland": "ash", "grass": "ash", "rock": "ash"}),
)

# Decoration spacing per biome (game tiles between sprites; None = bare).
DECOR = {
    "forest": (15, ("tree", "tree", "tree2")), "grass": (34, ("tree", "bush")),
    "lowland": (38, ("bush", "tree2")), "plains": (46, ("tuft", "bush")),
    "hills": (46, ("hill",)), "mountain": (36, ("mountain",)), "rock": (40, ("mountain", "rocks")),
    "dark": (30, ("deadtree", "rocks")), "desert": (48, ("dune", "dune", "cactus")),
    "snow": (22, ("pine", "pine", "snowpeak")), "swamp": (18, ("swamptree", "swamptree", "reeds")),
    "bog": (30, ("reeds", "swamptree")), "wild": (26, ("deadtree", "lava", "rocks")),
    "ash": (30, ("deadtree", "rocks", "lava")), "abyss": (None, ()),
}


# --------------------------------------------------------------------------
# rasters

def load_base(path: str) -> np.ndarray:
    e, m = geo.EXTENT, geo.WIKI_MAP
    s = m["px_per_tile"]
    im = Image.open(path).convert("RGB")
    box = ((e["x0"] - m["x_origin"]) * s, (m["y_top"] - e["y1"]) * s,
           (e["x1"] - m["x_origin"]) * s, (m["y_top"] - e["y0"]) * s)
    w, h = (e["x1"] - e["x0"]) // G, (e["y1"] - e["y0"]) // G
    return np.asarray(im.crop(box).resize((w, h), Image.BOX)).astype(int)


def to_grid(x: float, y: float) -> tuple:
    e = geo.EXTENT
    return (x - e["x0"]) / G, (e["y1"] - y) / G  # (col, row)


def land_mask(base: np.ndarray) -> np.ndarray:
    r, g, b = base[..., 0], base[..., 1], base[..., 2]
    blue = (b > r + 18) & (b > g + 8)
    # The Sailing "Backwater" south of Tirannwn is drawn grey-teal.
    grey_sea = (g - r > 8) & (b - r > 4) & (np.abs(g - b) < 18) & (base.max(-1) < 165)
    land = ~(blue | grey_sea)
    land = ndi.binary_opening(land, iterations=1)
    land = ndi.binary_closing(land, iterations=2)
    # Fill ponds and specks (keep real lakes), drop specks of land.
    holes = ndi.binary_fill_holes(land) & ~land
    lab, n = ndi.label(holes)
    if n:
        sizes = ndi.sum(holes, lab, range(1, n + 1))
        land |= np.isin(lab, 1 + np.nonzero(sizes < 90)[0])
    lab, n = ndi.label(land)
    sizes = ndi.sum(land, lab, range(1, n + 1))
    return np.isin(lab, 1 + np.nonzero(sizes >= 160)[0])


def disk(shape, cx, cy, r) -> np.ndarray:
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r


def biome_map(base: np.ndarray, land: np.ndarray) -> np.ndarray:
    blurred = np.asarray(Image.fromarray(base.astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(2))).astype(float)
    cent = np.array([c for c, _ in CENTRES], float)
    names = [n for _, n in CENTRES]
    d = ((blurred[..., None, :] - cent[None, None]) ** 2).sum(-1)
    idx = d.argmin(-1)
    name_idx = np.array([BIOMES.index(n) for n in names])[idx]
    # Blob it up: a majority filter turns speckle into cartoon patches.
    img = Image.fromarray(name_idx.astype(np.uint8))
    for _ in range(3):
        img = img.filter(ImageFilter.ModeFilter(7))
    out = np.asarray(img).astype(int)
    # Wobble the override boxes like the borders, so no straight edge shows.
    rng = np.random.default_rng(5)
    h, w = land.shape
    wx = ndi.gaussian_filter(rng.standard_normal((h, w)), 10)
    wy = ndi.gaussian_filter(rng.standard_normal((h, w)), 10)
    wx *= 8 / wx.std()
    wy *= 8 / wy.std()
    yy, xx = np.mgrid[:h, :w]
    XX, YY = xx + wx, yy + wy
    for (x0, x1, y0, y1), swap in OVERRIDES:
        c0, r1 = to_grid(x0, y0)
        c1, r0 = to_grid(x1, y1)
        box = (XX >= c0) & (XX < c1) & (YY >= r0) & (YY < r1)
        for src, dst in swap.items():
            out[box & (out == BIOMES.index(src))] = BIOMES.index(dst)
    out[~land] = -1
    return out


# --------------------------------------------------------------------------
# vectors

def _chaikin(pts: np.ndarray, rounds: int = 2) -> np.ndarray:
    for _ in range(rounds):
        a, b = pts, np.roll(pts, -1, axis=0)
        q, r = 0.75 * a + 0.25 * b, 0.25 * a + 0.75 * b
        pts = np.empty((len(a) * 2, 2))
        pts[0::2], pts[1::2] = q, r
    return pts


def mask_path(mask: np.ndarray, sigma: float = 1.1, tol: float = 0.45,
              min_len: int = 10, compact: bool = False) -> str:
    """Smooth outline(s) of a boolean mask as one SVG path (evenodd holes).
    ``compact`` trades a little smoothness for size: whole units and relative
    moves, for the territory shapes the site loads with every map."""
    f = ndi.gaussian_filter(np.pad(mask.astype(float), 2), sigma)
    parts = []
    for c in measure.find_contours(f, 0.5):
        if len(c) < min_len:
            continue
        c = measure.approximate_polygon(c, tol)
        if len(c) < 4:
            continue
        c = c[:-1] if np.allclose(c[0], c[-1]) else c
        if compact:
            c = _chaikin(c, rounds=1)
            pts = [(int(round((col - 2) * G)), int(round((row - 2) * G))) for row, col in c]
            pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
            if len(pts) < 3:
                continue
            rel = " ".join(f"{x - px} {y - py}" for (px, py), (x, y) in zip(pts, pts[1:]))
            parts.append(f"M{pts[0][0]} {pts[0][1]}l{rel}z")
            continue
        c = _chaikin(c)
        xy = [((col - 2) * G, (row - 2) * G) for row, col in c]
        parts.append("M" + " ".join(f"{x:.1f} {y:.1f}" for x, y in xy) + "Z")
    return "".join(parts)


# --------------------------------------------------------------------------
# territories

def build(world_png: str) -> dict:
    base = load_base(world_png)
    h, w = base.shape[:2]
    land = land_mask(base)
    biomes = biome_map(base, land)

    seeds, meta = [], []
    for region, key, x, y in geo.all_bosses():
        if region.get("inset"):
            continue
        seeds.append(to_grid(x, y))
        meta.append({"key": key, "region": region["key"]})
    # Grow every boss's patch of ground to a clickable island.
    for (cx, cy) in seeds:
        d = disk(land.shape, cx, cy, geo.MIN_ISLAND / G)
        biomes[d & ~land] = BIOMES.index("grass")
        land |= d

    # The Abyss rift island, two territories side by side.
    ax, ay = to_grid(*geo.ABYSS["center"])
    ar = geo.ABYSS["radius"] / G
    rift = disk(land.shape, ax, ay, ar)
    land |= rift
    biomes[rift] = BIOMES.index("abyss")
    for i, (region, key, _x, _y) in enumerate(
            [b for b in geo.all_bosses() if b[0].get("inset")]):
        seeds.append((ax + (-0.42 if i == 0 else 0.42) * ar, ay + (0.1 if i else -0.1) * ar))
        meta.append({"key": key, "region": region["key"]})

    reach = ndi.binary_dilation(land, iterations=3)
    yy, xx = np.mgrid[:h, :w]
    # One smooth random warp shared by kingdom edges and territory borders,
    # so every border meanders like a hand-drawn one.
    rng = np.random.default_rng(3)

    def field(amp, sigma):
        f = ndi.gaussian_filter(rng.standard_normal((h, w)), sigma)
        return f * (amp / f.std())

    wx, wy = field(9, 14), field(9, 14)
    XX, YY = xx + wx, yy + wy
    area = _area_raster(h, w)
    area_w = ndi.map_coordinates(area, [np.clip(YY, 0, h - 1), np.clip(XX, 0, w - 1)],
                                 order=0, mode="nearest")
    region_keys = [r["key"] for r in geo.REGIONS]
    # Every bit of land belongs to a region: a kingdom's own ground first,
    # then anything outside every kingdom (Karamja, Feldip, the islands) goes
    # to whichever kingdom is nearest.
    ground_all = reach & ~disk((h, w), ax, ay, ar + 6)
    rid = np.where(ground_all, area_w, -1)
    rid = _fill_by_landmass(rid, ground_all)
    rid[reach & disk((h, w), ax, ay, ar + 3)] = region_keys.index("abyss")

    # 1. Where each boss's territory should centre: Lloyd relaxation inside
    #    its region from the real spots, so territories even out in size.
    #    Each round is pulled partway back to the real spot, so a territory
    #    grows around where its boss lives instead of wandering off. Only the
    #    landmasses a boss stands on are carved; a region's other islands
    #    join the nearest territory at the end.
    targets = list(seeds)
    mains, home_comp = {}, {}
    for k, rk in enumerate(region_keys):
        idx = [i for i, m in enumerate(meta) if m["region"] == rk]
        ground = rid == k
        if not idx or not ground.any():
            continue
        comps, _n = ndi.label(ground)
        _, (cri, cci) = ndi.distance_transform_edt(comps == 0, return_indices=True)
        for i in idx:
            r0, c0 = int(round(seeds[i][1])), int(round(seeds[i][0]))
            home_comp[i] = comps == comps[cri[r0, c0], cci[r0, c0]]
        mains[k] = np.any([home_comp[i] for i in idx], axis=0)
        # Each landmass is balanced on its own, between the bosses on it.
        for piece in {id(home_comp[i]): home_comp[i] for i in idx}.values():
            on = [i for i in idx if np.array_equal(home_comp[i], piece)]
            gy, gx = np.nonzero(piece)
            wgx, wgy = XX[gy, gx], YY[gy, gx]
            pts = np.array([seeds[i] for i in on], float)
            true = pts.copy()
            for _ in range(LLOYD_ROUNDS):
                own = np.hypot(wgx[None] - pts[:, :1], wgy[None] - pts[:, 1:]).argmin(0)
                for n in range(len(on)):
                    sel = own == n
                    if sel.any():
                        centre = np.array((gx[sel].mean(), gy[sel].mean()))
                        pts[n] = (1 - LLOYD_ANCHOR) * centre + LLOYD_ANCHOR * true[n]
            for n, i in enumerate(on):
                targets[i] = (float(pts[n][0]), float(pts[n][1]))

    # 2. Region names: each gets the roomiest spot near its region's middle.
    name_spots, reserved = {}, np.zeros((h, w), bool)
    for k, region in enumerate(geo.REGIONS):
        if region.get("inset"):
            continue
        spot, box = _name_spot((rid == k) & land, region["name"], reserved)
        name_spots[region["key"]] = spot
        if box is not None:
            reserved |= box

    # 3. Badges: each at the free spot nearest its territory's centre, clear
    #    of the names and of every badge placed before it (the most crowded
    #    regions go first, while there is still room).
    labels = [geo.label_for(m["key"]) for m in meta]
    halfw = [geo.badge_half_width(t) for t in labels]
    tile_region = [region_keys.index(m["region"]) for m in meta]
    crowding = {k: sum(1 for t in tile_region if t == k) / max(int(mains[k].sum()), 1)
                for k in mains}
    anchors = [None] * len(seeds)
    for i in sorted(range(len(seeds)), key=lambda i: -crowding.get(tile_region[i], 0)):
        anchors[i] = _place_badge(targets[i], halfw[i], reserved, home_comp[i], land, reach)
        reserved |= _badge_box(anchors[i], halfw[i], xx, yy, pad=6)

    # 4. Territories around the badges: every region split between its
    #    badges (so each badge is always inside its own territory, near its
    #    middle), islands to the nearest territory.
    label = np.full((h, w), -1)
    drop = (BADGE_DOWN - BADGE_UP) / 2  # a badge's middle sits below its medallion
    for k, rk in enumerate(region_keys):
        idx = [i for i, m in enumerate(meta) if m["region"] == rk]
        if not idx or k not in mains:
            continue
        ground, main = rid == k, mains[k]
        for piece in {id(home_comp[i]): home_comp[i] for i in idx}.values():
            on = [i for i in idx if np.array_equal(home_comp[i], piece)]
            gy, gx = np.nonzero(piece)
            wgx, wgy = XX[gy, gx], YY[gy, gx]
            pts = np.array([(anchors[i][0] / G, (anchors[i][1] + drop) / G) for i in on])
            own = np.hypot(wgx[None] - pts[:, :1], wgy[None] - pts[:, 1:]).argmin(0)
            label[gy, gx] = np.array(on)[own]
        rest = ground & ~main
        if rest.any():
            _, (iri, ici) = ndi.distance_transform_edt(~main, return_indices=True)
            label[rest] = label[iri, ici][rest]

    # 5. Bend the borders around every medallion and its scroll: the ground
    #    under each one belongs to its own territory, so no border (between
    #    territories or regions) ever runs underneath a name.
    for i, (x, y) in enumerate(anchors):
        a, b = (halfw[i] + 12) / G, ((BADGE_UP + BADGE_DOWN) / 2 + 10) / G
        box = (np.abs((xx - x / G) / a) ** 4 + np.abs((yy - (y + drop) / G) / b) ** 4) <= 1
        label[box & reach] = i
    # Slivers the wobble or a stamp cuts off go to their neighbour, so every
    # territory is one piece of land (plus whole islands).
    for i in range(len(seeds)):
        m = label == i
        lab, n = ndi.label(m)
        if n > 1:
            sizes = ndi.sum(m, lab, range(1, n + 1))
            label[m & np.isin(lab, 1 + np.nonzero(sizes < 60)[0])] = -1
    hole = reach & (label < 0)
    if hole.any():
        _, (ri, ci) = ndi.distance_transform_edt(label < 0, return_indices=True)
        label[hole] = label[ri, ci][hole]

    # Neighbours: territories sharing a border.
    pairs = {}
    for a, b in ((label[:, :-1], label[:, 1:]), (label[:-1, :], label[1:, :])):
        edge = (a != b) & (a >= 0) & (b >= 0)
        for u, v in zip(a[edge], b[edge]):
            k = (min(u, v), max(u, v))
            pairs[k] = pairs.get(k, 0) + 1
    edges = sorted((meta[u]["key"], meta[v]["key"]) for (u, v), n in pairs.items() if n >= 4)

    tiles = []
    for i, m in enumerate(meta):
        cell = label == i
        area = int(cell.sum()) * G * G
        tiles.append({**m, "label": labels[i], "path": mask_path(cell, sigma=1.2, tol=0.7, compact=True),
                      "x": round(anchors[i][0], 1), "y": round(anchors[i][1], 1),
                      "true": [round(float(seeds[i][0]) * G, 1), round(float(seeds[i][1]) * G, 1)],
                      "area": area})

    regions = []
    for region in geo.REGIONS:
        idx = [i for i, m in enumerate(meta) if m["region"] == region["key"]]
        union = np.isin(label, idx)
        spot = name_spots.get(region["key"])
        regions.append({"key": region["key"], "name": region["name"], "color": region["color"],
                        "path": mask_path(union, sigma=1.2, tol=0.7, compact=True),
                        "label": spot})

    biome_paths = {}
    grow = ndi.binary_dilation(land, iterations=2)
    for bi, name in enumerate(BIOMES):
        m = biomes == bi
        if m.sum() < 8:
            continue
        # Let each patch run a little past the coast; the land clip trims it.
        m = m | (grow & ~land & (ndi.grey_dilation(np.where(biomes == bi, 1, 0), size=5) > 0))
        biome_paths[name] = mask_path(m, sigma=1.4, tol=0.6, min_len=14)

    decor = _decorations(biomes, land, anchors, halfw, regions, h, w)
    waves = _waves(land, h, w)
    e = geo.EXTENT
    return {
        "name": "Gielinor", "units": "game tiles",
        "badge": geo.BADGE, "region_font": geo.REGION_FONT,
        "width": e["x1"] - e["x0"], "height": e["y1"] - e["y0"], "extent": e,
        "land": mask_path(land, sigma=1.0, tol=0.4),
        "biomes": biome_paths,
        "regions": regions, "tiles": tiles, "edges": edges,
        "abyss": {"x": round(ax * G, 1), "y": round(ay * G, 1), "r": geo.ABYSS["radius"],
                  "tether": [round(v * G, 1) for v in to_grid(*geo.ABYSS["tether"])]},
        "decor": decor, "waves": waves,
    }


def _area_raster(h, w) -> np.ndarray:
    """Region index per working pixel from geo.AREAS (-1 = no kingdom)."""
    from PIL import ImageDraw
    keys = [r["key"] for r in geo.REGIONS]
    img = Image.new("I", (w, h), -1)
    draw = ImageDraw.Draw(img)
    for rk in reversed(geo.AREA_PRIORITY):  # highest priority painted last
        for poly in geo.AREAS[rk]:
            draw.polygon([to_grid(x, y) for x, y in poly], fill=keys.index(rk))
    return np.asarray(img).astype(int)


LLOYD_ROUNDS = 12
LLOYD_ANCHOR = 0.55


def _fill_by_landmass(rid, ground):
    """Give every unassigned bit of land a region. Land joined to a kingdom
    takes the nearest kingdom on the same landmass; an island with no
    kingdom on it goes whole to the nearest kingdom across the water."""
    lab, n = ndi.label(ground)
    known = rid >= 0
    _, (ri, ci) = ndi.distance_transform_edt(~known, return_indices=True)
    dist_known = ndi.distance_transform_edt(~known)
    out = rid.copy()
    objs = ndi.find_objects(lab)
    for k, sl in enumerate(objs, start=1):
        comp = lab[sl] == k
        sub_known = known[sl] & comp
        todo = comp & ~sub_known
        if not todo.any():
            continue
        if sub_known.any():
            _, (sri, sci) = ndi.distance_transform_edt(~sub_known, return_indices=True)
            vals = rid[sl][sri, sci]
            out[sl][todo] = vals[todo]
        else:
            d = np.where(comp, dist_known[sl], np.inf)
            r, c = np.unravel_index(d.argmin(), d.shape)
            out[sl][todo] = rid[ri[sl][r, c], ci[sl][r, c]]
    return out


def _badge_box(anchor, halfw, xx, yy, pad=0.0):
    x, y = anchor
    return ((np.abs(xx * G - x) <= halfw + pad)
            & (yy * G >= y - BADGE_UP - pad) & (yy * G <= y + BADGE_DOWN + pad))


# Space a boss badge takes around its anchor (board units, from geo.BADGE):
# BADGE_UP above and BADGE_DOWN below; the width comes from the name.
BADGE_UP, BADGE_DOWN = geo.BADGE["up"], geo.BADGE["down"]


def _place_badge(target, halfw, reserved, main, land, reach):
    """The free spot nearest ``target`` (grid coords) where a whole badge
    fits: the medallion on ``main`` (the landmass the boss stands on, within
    its region), the scroll over that land or the sea, nothing overlapping a
    name or another badge. Roomier spots are tried first."""
    h, w = main.shape
    yy, xx = np.mgrid[:h, :w]
    dist = np.hypot(xx - target[0], yy - target[1])
    off = int(round((BADGE_DOWN - BADGE_UP) / 2 / G))

    def nearest_fit(allowed, stand, margin):
        cols = int(math.ceil(2 * (halfw + margin) / G)) | 1
        rows = int(math.ceil((BADGE_UP + BADGE_DOWN + 2 * margin) / G)) | 1
        fit = ndi.minimum_filter(allowed.astype(np.uint8), size=(rows, cols), mode="constant") > 0
        fit = np.roll(fit, -off, axis=0)
        fit[-off:] = False
        fit &= stand
        if not fit.any():
            return None
        r, c = np.unravel_index(np.where(fit, dist, np.inf).argmin(), dist.shape)
        return (float(c * G), float(r * G)), float(dist[r, c]) * G

    allowed = (main | ~reach) & ~reserved
    for margin in (40, 26, 14, 6, 0):
        got = nearest_fit(allowed, main & land, margin)
        if got and got[1] <= 160:
            return got[0]
    got = nearest_fit(allowed, main & land, 0) or nearest_fit(~reserved, np.ones_like(main), 0)
    return got[0] if got else (float(target[0] * G), float(target[1] * G))


def _name_spot(mask, name, reserved):
    """Where a region's name goes, and the box it (plus the ownership pips
    the site draws under it) takes: the spot nearest the region's middle
    where the whole box fits on the region's own land, as roomy as can be."""
    if not mask.any():
        return None, None
    h, w = mask.shape
    cy, cx = ndi.center_of_mass(mask)
    half = geo.region_label_half_width(name)
    fs = geo.REGION_FONT
    top, bottom = fs + 4, fs * 0.8  # glyphs above the baseline, pips below
    off = int(round((bottom - top) / 2 / G))  # box centre below the baseline
    free = mask & ~reserved
    yy, xx = np.mgrid[:h, :w]
    for margin in (36, 24, 14, 6, 0):
        cols = int(math.ceil(2 * (half + margin) / G)) | 1
        rows = int(math.ceil((top + bottom + 2 * margin) / G)) | 1
        fit = ndi.minimum_filter(free.astype(np.uint8), size=(rows, cols), mode="constant") > 0
        fit = np.roll(fit, -off, axis=0)
        if off > 0:
            fit[-off:] = False
        if fit.any():
            d = np.where(fit, np.hypot(xx - cx, yy - cy), np.inf)
            r, c = np.unravel_index(d.argmin(), d.shape)
            break
    else:
        r, c = int(cy), int(cx)
    x, y = c * G, r * G
    box = ((xx * G >= x - half) & (xx * G <= x + half)
           & (yy * G >= y - top) & (yy * G <= y + bottom))
    return [round(float(x), 1), round(float(y), 1)], box


def _poisson(cands_mask, radius_px, rng, taken=None, limit=4000):
    ys, xs = np.nonzero(cands_mask)
    if not len(xs):
        return []
    order = rng.permutation(len(xs))[:limit * 6]
    out = taken if taken is not None else []
    cell = radius_px
    grid = {}
    for (x, y) in out:
        grid.setdefault((int(x // cell), int(y // cell)), []).append((x, y))
    got = []
    for k in order:
        x, y = xs[k] + rng.random() - 0.5, ys[k] + rng.random() - 0.5
        gx, gy = int(x // cell), int(y // cell)
        ok = True
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (px, py) in grid.get((gx + dx, gy + dy), ()):
                    if (px - x) ** 2 + (py - y) ** 2 < radius_px ** 2:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            grid.setdefault((gx, gy), []).append((x, y))
            got.append((x, y))
            if len(got) >= limit:
                break
    return got


def _decorations(biomes, land, anchors, halfw, regions, h, w):
    rng = np.random.default_rng(7)
    inland = ndi.distance_transform_edt(land) > 5 / G
    keep_clear = np.zeros_like(land)
    for (x, y), hw in zip(anchors, halfw):
        keep_clear[int(max((y - BADGE_UP - 8) / G, 0)):int((y + BADGE_DOWN) / G),
                   int(max((x - hw - 4) / G, 0)):int((x + hw + 4) / G)] = True
    for reg in regions:
        if reg["label"]:
            lx, ly = reg["label"][0] / G, reg["label"][1] / G
            half = geo.region_label_half_width(reg["name"]) / G
            fs = geo.REGION_FONT
            keep_clear[int(max(ly - (fs + 6) / G, 0)):int(ly + fs * 0.8 / G),
                       int(max(lx - half, 0)):int(lx + half)] = True
    out = []
    for bi, name in enumerate(BIOMES):
        spacing, kinds = DECOR.get(name, (None, ()))
        if not spacing:
            continue
        m = (biomes == bi) & inland & ~keep_clear
        for (x, y) in _poisson(m, spacing / G, rng):
            kind = kinds[int(rng.integers(len(kinds)))]
            out.append([kind, round(float(x) * G, 1), round(float(y) * G, 1),
                        round(float(0.8 + 0.4 * rng.random()), 2)])
    out.sort(key=lambda d: d[2])  # paint north to south so sprites overlap right
    return out


def _waves(land, h, w):
    rng = np.random.default_rng(11)
    open_sea = ndi.distance_transform_edt(~land) > 20 / G
    return [[round(float(x) * G, 1), round(float(y) * G, 1)] for x, y in _poisson(open_sea, 70 / G, rng, limit=400)]


if __name__ == "__main__":
    data = build(sys.argv[1])
    with open(sys.argv[2], "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"{len(data['tiles'])} tiles, {len(data['edges'])} borders, "
          f"{len(data['decor'])} decorations, {sum(len(v) for v in data['biomes'].values()) // 1024} KiB biomes")
