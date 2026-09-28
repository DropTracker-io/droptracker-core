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


# (row, col) where the cropped board starts in the full grid (build sets it).
_OFF = [0, 0]


def to_grid(x: float, y: float) -> tuple:
    e = geo.EXTENT
    return (x - e["x0"]) / G - _OFF[1], (e["y1"] - y) / G - _OFF[0]  # (col, row)


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
              min_len: int = 10, compact: bool = False, origin=(0, 0)) -> str:
    """Smooth outline(s) of a boolean mask as one SVG path (evenodd holes).
    ``compact`` trades a little smoothness for size: whole units and relative
    moves, for the territory shapes the site loads with every map. ``origin``
    is the (row, col) a cropped mask starts at in the full grid."""
    r0, c0 = origin
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
            pts = [(int(round((col - 2 + c0) * G)), int(round((row - 2 + r0) * G))) for row, col in c]
            pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
            if len(pts) < 3:
                continue
            rel = " ".join(f"{x - px} {y - py}" for (px, py), (x, y) in zip(pts, pts[1:]))
            parts.append(f"M{pts[0][0]} {pts[0][1]}l{rel}z")
            continue
        c = _chaikin(c)
        xy = [((col - 2 + c0) * G, (row - 2 + r0) * G) for row, col in c]
        parts.append("M" + " ".join(f"{x:.1f} {y:.1f}" for x, y in xy) + "Z")
    return "".join(parts)


# --------------------------------------------------------------------------
# territories

def build(world_png: str, regions=None, drop_tiles=(), variants: bool = False) -> dict:
    """The board's geometry. ``regions`` (None = all) are the region keys to
    keep and ``drop_tiles`` the tile keys to leave out of them.

    Leaving things out regenerates the whole board: land of a removed region
    goes to the kept regions on the same landmass, a landmass with no kept
    region left is dropped (drawn as sea), and the frame is cropped to what
    is left. Open water is shared out between the sea tiles, so crossing
    water always means going through a sea tile; parts of the map that still
    can't reach each other (no sea tiles kept) are linked by their shortest
    crossing (``edges``)."""
    keep = {r["key"] for r in geo.REGIONS} if regions is None else set(regions)
    drop = set(drop_tiles)
    bosses = [b for b in geo.all_bosses() if b[0]["key"] in keep and b[1] not in drop]
    kept_regions = {b[0]["key"] for b in bosses}
    land_bosses = [b for b in bosses if not b[0].get("inset") and not b[0].get("sea")]
    abyss_bosses = [b for b in bosses if b[0].get("inset")]
    sea_bosses = [b for b in bosses if b[0].get("sea")]
    region_keys = [r["key"] for r in geo.REGIONS]
    grass = BIOMES.index("grass")

    # ---- phase A: the whole map, to decide what stays and crop to it ----
    _OFF[0] = _OFF[1] = 0
    base = load_base(world_png)
    H0, W0 = base.shape[:2]
    land = land_mask(base)
    biomes = biome_map(base, land)
    for _r, _k, x, y in land_bosses:
        d = disk(land.shape, *to_grid(x, y), geo.MIN_ISLAND / G)
        biomes[d & ~land] = grass
        land |= d
    ax0, ay0 = to_grid(*geo.ABYSS["center"])
    ar = geo.ABYSS["radius"] / G
    rift_full = disk(land.shape, ax0, ay0, ar)
    reach = ndi.binary_dilation(land, iterations=3)
    ground_all = reach & ~disk((H0, W0), ax0, ay0, ar + 6)
    # Which kingdom every bit of land belongs to on the full map; a landmass
    # none of whose land is in a kept region goes.
    rid_full = _fill_by_landmass(np.where(ground_all, _area_raster(H0, W0), -1), ground_all)
    kept_idx = [region_keys.index(k) for k in kept_regions
                if not geo.REGION_BY_KEY[k].get("sea") and not geo.REGION_BY_KEY[k].get("inset")]
    # Landmasses by the land itself, rivers bridged (the sea strait to an
    # island is far wider than a river).
    comps, _n = ndi.label(ndi.binary_dilation(land, iterations=RIVER_BRIDGE))
    live = np.unique(comps[np.isin(rid_full, kept_idx) & land & (comps > 0)])
    gone = reach & ~np.isin(comps, live)
    land &= ~gone
    reach &= ~gone
    biomes[gone] = -1
    keep_box = land.copy()
    if abyss_bosses:
        keep_box |= rift_full
    if not keep_box.any():  # only the seas: frame their spots
        for _r, _k, x, y in sea_bosses:
            keep_box |= disk(land.shape, *to_grid(x, y), 60 / G)
    rows, cols = np.nonzero(keep_box)
    margin = CROP_MARGIN / G
    r0, r1 = max(int(rows.min() - margin), 0), min(int(rows.max() + margin), H0)
    c0, c1 = max(int(cols.min() - margin), 0), min(int(cols.max() + margin), W0)

    # ---- phase B: the cropped board ----
    land, biomes, reach = land[r0:r1, c0:c1], biomes[r0:r1, c0:c1], reach[r0:r1, c0:c1]
    _OFF[0], _OFF[1] = r0, c0
    h, w = land.shape

    seeds, meta = [], []
    for region, key, x, y in land_bosses:
        seeds.append(to_grid(x, y))
        meta.append({"key": key, "region": region["key"]})

    # The Abyss rift island, two territories side by side (or one).
    ax, ay = to_grid(*geo.ABYSS["center"])
    if abyss_bosses:
        rift = disk(land.shape, ax, ay, ar)
        land |= rift
        reach |= ndi.binary_dilation(rift, iterations=3)
        biomes[rift] = BIOMES.index("abyss")
        for i, (region, key, _x, _y) in enumerate(abyss_bosses):
            dx = 0.0 if len(abyss_bosses) == 1 else (-0.42 if i == 0 else 0.42)
            seeds.append((ax + dx * ar, ay + (0.0 if dx == 0 else (0.1 if i else -0.1)) * ar))
            meta.append({"key": key, "region": region["key"]})

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
    # Every bit of land belongs to a kept region: a kingdom's own ground
    # first, then anything outside every kept kingdom (a removed region's
    # land, Feldip, the islands) goes to the nearest kept kingdom on it.
    ground_all = reach & ~disk((h, w), ax, ay, ar + 6)
    rid = np.where(ground_all & np.isin(area_w, kept_idx), area_w, -1)
    rid = _fill_by_landmass(rid, ground_all)
    if abyss_bosses:
        rid[reach & disk((h, w), ax, ay, ar + 3)] = region_keys.index("abyss")

    # 1. Where each boss's territory should centre: Lloyd relaxation inside
    #    its region from the real spots, so territories even out in size.
    #    Each round is pulled partway back to the real spot, so a territory
    #    grows around where its boss lives instead of wandering off. Only the
    #    landmasses a boss stands on are carved; a region's other islands
    #    join the sea around them (or the nearest territory, with no seas).
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
            rr = min(max(int(round(seeds[i][1])), 0), h - 1)
            cc = min(max(int(round(seeds[i][0])), 0), w - 1)
            home_comp[i] = comps == comps[cri[rr, cc], cci[rr, cc]]
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
        if region["key"] not in kept_regions or region.get("inset") or region.get("sea"):
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
    #    middle).
    label = np.full((h, w), -1)
    drop_ = (BADGE_DOWN - BADGE_UP) / 2  # a badge's middle sits below its medallion
    rest_all = np.zeros((h, w), bool)
    for k, rk in enumerate(region_keys):
        idx = [i for i, m in enumerate(meta) if m["region"] == rk]
        if not idx or k not in mains:
            continue
        ground, main = rid == k, mains[k]
        for piece in {id(home_comp[i]): home_comp[i] for i in idx}.values():
            on = [i for i in idx if np.array_equal(home_comp[i], piece)]
            gy, gx = np.nonzero(piece)
            wgx, wgy = XX[gy, gx], YY[gy, gx]
            pts = np.array([(anchors[i][0] / G, (anchors[i][1] + drop_) / G) for i in on])
            own = np.hypot(wgx[None] - pts[:, :1], wgy[None] - pts[:, 1:]).argmin(0)
            label[gy, gx] = np.array(on)[own]
        rest_all |= ground & ~main
    if not sea_bosses and rest_all.any():
        owned = label >= 0
        _, (iri, ici) = ndi.distance_transform_edt(~owned, return_indices=True)
        label[rest_all] = label[iri, ici][rest_all]

    # 5. Bend the borders around every medallion and its scroll: the ground
    #    under each one belongs to its own territory, so no border (between
    #    territories or regions) ever runs underneath a name.
    for i, (x, y) in enumerate(anchors):
        a, b = (halfw[i] + 12) / G, ((BADGE_UP + BADGE_DOWN) / 2 + 10) / G
        box = (np.abs((xx - x / G) / a) ** 4 + np.abs((yy - (y + drop_) / G) / b) ** 4) <= 1
        label[box & reach & ~rest_all] = i
    # Slivers the wobble or a stamp cuts off go to their neighbour, so every
    # territory is one piece of land (plus whole islands).
    for i in range(len(seeds)):
        m = label == i
        lab, n = ndi.label(m)
        if n > 1:
            sizes = ndi.sum(m, lab, range(1, n + 1))
            label[m & np.isin(lab, 1 + np.nonzero(sizes < 60)[0])] = -1
    hole = reach & (label < 0) & ~(rest_all if sea_bosses else np.zeros_like(rest_all))
    if hole.any() and (label >= 0).any():
        _, (ri, ci) = ndi.distance_transform_edt(label < 0, return_indices=True)
        label[hole] = label[ri, ci][hole]

    # 6. The seas: all the open water (and the islands no boss stands on) is
    #    shared out between the sea tiles, each growing out through the water
    #    from its spot, so a sea never jumps across land. Rivers and narrow
    #    channels stay out (they'd snake a sea deep inland); what the seas
    #    touch is what they border, so every crossing goes through one.
    sea_labels = {}
    if sea_bosses:
        from skimage.segmentation import watershed

        water = ~reach | rest_all
        core = ndi.binary_opening(~reach, iterations=SEA_OPEN) | rest_all
        lab, _count = ndi.label(core)
        _, (wri, wci) = ndi.distance_transform_edt(~core, return_indices=True)
        markers = np.zeros((h, w), np.int32)
        centres, reached = [], set()
        for n, (_r, _k, x, y) in enumerate(sea_bosses):
            cx, cy = to_grid(x, y)
            rr = min(max(int(round(cy)), 0), h - 1)
            cc = min(max(int(round(cx)), 0), w - 1)
            rr, cc = int(wri[rr, cc]), int(wci[rr, cc])
            while markers[rr, cc]:  # two spots on one pixel: nudge
                cc = min(cc + 1, w - 1)
            markers[rr, cc] = n + 1
            reached.add(lab[rr, cc])
            centres.append((cc, rr))
        sea = watershed(np.zeros((h, w)), markers, mask=core & np.isin(lab, list(reached)))
        for n, (region, key, _x, _y) in enumerate(sea_bosses):
            patch = sea == n + 1
            i = len(meta)
            meta.append({"key": key, "region": region["key"]})
            seeds.append(centres[n])
            labels.append(geo.label_for(key))
            halfw.append(geo.badge_half_width(labels[i]))
            anchors.append(_place_badge(centres[n], halfw[i], reserved, patch & ~reach,
                                        patch & ~reach, reach))
            reserved |= _badge_box(anchors[i], halfw[i], xx, yy, pad=6)
            label[patch] = i
        for n, (region, key, _x, _y) in enumerate(sea_bosses):
            spot, _box = _name_spot((sea == n + 1) & water, region["name"], reserved)
            if spot:
                sea_labels.setdefault(region["key"], {})[key] = spot
        for rk, spots in sea_labels.items():
            name_spots[rk] = next(iter(spots.values()))

    # Neighbours: territories sharing a border, then the shortest crossing
    # between any parts of the map that still can't reach each other.
    pairs = {}
    for a, b in ((label[:, :-1], label[:, 1:]), (label[:-1, :], label[1:, :])):
        edge = (a != b) & (a >= 0) & (b >= 0)
        for u, v in zip(a[edge], b[edge]):
            k = (min(u, v), max(u, v))
            pairs[k] = pairs.get(k, 0) + 1
    links = {k for k, n in pairs.items() if n >= 4}
    # The Abyss is entered from the Wilderness (the tether on the map): its
    # nearest tile borders whatever territory holds the tether's end.
    abyss_ids = [i for i, m in enumerate(meta) if geo.REGION_BY_KEY[m["region"]].get("inset")]
    if abyss_ids:
        tx, ty = to_grid(*geo.ABYSS["tether"])
        tr = min(max(int(round(ty)), 0), h - 1)
        tc = min(max(int(round(tx)), 0), w - 1)
        owned = label >= 0
        if owned.any():
            _, (tri, tci) = ndi.distance_transform_edt(~owned, return_indices=True)
            at = int(label[tri[tr, tc], tci[tr, tc]])
            if at not in abyss_ids:
                near = min(abyss_ids, key=lambda i: math.hypot(anchors[i][0] / G - tx,
                                                             anchors[i][1] / G - ty))
                links.add((min(at, near), max(at, near)))
    links |= _bridges(label, len(meta), links)
    edges = sorted(tuple(sorted((meta[u]["key"], meta[v]["key"]))) for u, v in links)

    tiles = []
    for i, m in enumerate(meta):
        cell = label == i
        area_px = int(cell.sum()) * G * G
        tiles.append({**m, "label": labels[i], "path": mask_path(cell, sigma=1.2, tol=0.7, compact=True),
                      "x": round(anchors[i][0], 1), "y": round(anchors[i][1], 1),
                      "true": [round(float(seeds[i][0]) * G, 1), round(float(seeds[i][1]) * G, 1)],
                      "area": area_px})

    regions_out = []
    for region in geo.REGIONS:
        if region["key"] not in kept_regions:
            continue
        idx = [i for i, m in enumerate(meta) if m["region"] == region["key"]]
        union = np.isin(label, idx)
        spot = name_spots.get(region["key"])
        regions_out.append({"key": region["key"], "name": region["name"], "color": region["color"],
                            "path": mask_path(union, sigma=1.2, tol=0.7, compact=True),
                            "label": spot, "sea": bool(region.get("sea")),
                            **({"labels": sea_labels[region["key"]]}
                               if region["key"] in sea_labels else {})})
    var, shapes = ({}, [])
    if variants:
        var, shapes = _variants(label, meta, {r["key"] for r in geo.REGIONS if r.get("sea")})

    biome_paths = {}
    grow = ndi.binary_dilation(land, iterations=2)
    for bi, name in enumerate(BIOMES):
        m = biomes == bi
        if m.sum() < 8:
            continue
        # Let each patch run a little past the coast; the land clip trims it.
        m = m | (grow & ~land & (ndi.grey_dilation(np.where(biomes == bi, 1, 0), size=5) > 0))
        biome_paths[name] = mask_path(m, sigma=1.4, tol=0.6, min_len=14)

    decor = _decorations(biomes, land, anchors, halfw, regions_out, h, w)
    waves = _waves(land, h, w)
    e = geo.EXTENT
    extent = {"x0": e["x0"] + c0 * G, "x1": e["x0"] + c1 * G,
              "y0": e["y1"] - r1 * G, "y1": e["y1"] - r0 * G}
    abyss = None
    if abyss_bosses:
        abyss = {"x": round(ax * G, 1), "y": round(ay * G, 1), "r": geo.ABYSS["radius"],
                 "tether": [round(v * G, 1) for v in to_grid(*geo.ABYSS["tether"])]}
    return {
        "name": "Gielinor", "units": "game tiles",
        "badge": geo.BADGE, "region_font": geo.REGION_FONT,
        "width": w * G, "height": h * G, "extent": extent,
        "selection": {"regions": sorted(kept_regions), "drop": sorted(drop)},
        "land": mask_path(land, sigma=1.0, tol=0.4),
        "biomes": biome_paths,
        "regions": regions_out, "tiles": tiles, "edges": [list(e_) for e_ in edges],
        "variants": var, "shapes": shapes,
        "abyss": abyss,
        "decor": decor, "waves": waves,
    }


def _bridges(label, count, links) -> set:
    """Extra links joining parts of the map that can't reach each other:
    repeatedly link the part holding the first tile to the nearest other
    part, through the closest pair of territories (the shortest crossing)."""
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for u, v in links:
        parent[find(u)] = find(v)
    present = [i for i in range(count) if (label == i).any()]
    out = set()
    while len({find(i) for i in present}) > 1:
        root = find(present[0])
        mine = np.isin(label, [i for i in present if find(i) == root])
        dist, (ri, ci) = ndi.distance_transform_edt(~mine, return_indices=True)
        other = (label >= 0) & ~mine
        d = np.where(other, dist, np.inf)
        r, c = np.unravel_index(d.argmin(), d.shape)
        u, v = int(label[ri[r, c], ci[r, c]]), int(label[r, c])
        out.add((min(u, v), max(u, v)))
        parent[find(u)] = find(v)
    return out


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


def _variants(label, meta, sea_regions):
    """Every region re-cut for every way of leaving some of its tiles out.

    ``({region: {"tiles": [keys], "masks": {bits: {tile key: shape index}}}},
    shapes)``: ``bits`` says which of the region's tiles stay (bit n = its nth
    tile); only the tiles whose territory changed are listed, pointing into
    the shared ``shapes`` list. A kept tile keeps every bit of ground it had
    and grows into the ground of the tiles left out, nearest first (with a
    little wobble so the new borders meander like the old ones), so the
    region's outline, and every other region, stays exactly as drawn.

    Sea regions have no variants: their patches never touch, so a left-out
    patch is just open sea and the outline is the kept patches'. Leaving a
    region out entirely needs none either: it is simply not drawn."""
    h, w = label.shape
    rng = np.random.default_rng(13)
    noise = ndi.gaussian_filter(rng.standard_normal((h, w)), 10)
    noise *= 4 / noise.std()
    shapes, index = [], {}

    def shape_id(mask, origin):
        path = mask_path(mask, sigma=1.2, tol=0.7, compact=True, origin=origin)
        if path not in index:
            index[path] = len(shapes)
            shapes.append(path)
        return index[path]

    out = {}
    region_keys = []
    for m in meta:
        if m["region"] not in region_keys:
            region_keys.append(m["region"])
    for rk in region_keys:
        idx = [i for i, m in enumerate(meta) if m["region"] == rk]
        n = len(idx)
        if n < 2 or rk in sea_regions:
            continue
        ground = np.isin(label, idx)
        rows, cols = np.nonzero(ground)
        r0, c0 = max(int(rows.min()) - 6, 0), max(int(cols.min()) - 6, 0)
        r1, c1 = min(int(rows.max()) + 7, h), min(int(cols.max()) + 7, w)
        sub, g = label[r0:r1, c0:c1], ground[r0:r1, c0:c1]
        base = {i: sub == i for i in idx}
        dist = np.stack([ndi.distance_transform_edt(~base[i]) for i in idx])
        dist += noise[r0:r1, c0:c1][None]
        masks = {}
        for bits in range(1, (1 << n) - 1):
            keep = [b for b in range(n) if bits >> b & 1]
            kept = [idx[b] for b in keep]
            grow = np.array(kept)[dist[keep].argmin(0)]
            new = np.where(g, np.where(np.isin(sub, kept), sub, grow), -1)
            # A grown piece cut off from its tile that is only a sliver goes
            # to whoever surrounds it, as in the full map.
            for i in kept:
                lab, count = ndi.label(new == i)
                if count > 1:
                    sizes = ndi.sum(new == i, lab, range(1, count + 1))
                    small = 1 + np.nonzero(sizes < 60)[0]
                    new[np.isin(lab, small) & ~base[i]] = -1
            hole = g & (new < 0)
            if hole.any():
                _, (ri, ci) = ndi.distance_transform_edt(new < 0, return_indices=True)
                new[hole] = new[ri, ci][hole]
            entry = {}
            for i in kept:
                cell = new == i
                if not np.array_equal(cell, base[i]):
                    entry[meta[i]["key"]] = shape_id(cell, (r0, c0))
            masks[str(bits)] = entry
        out[rk] = {"tiles": [meta[i]["key"] for i in idx], "masks": masks}
    return out, shapes


LLOYD_ROUNDS = 12
LLOYD_ANCHOR = 0.55
# Water narrower than about twice this (working pixels) is a river or a
# channel, not open sea: the seas stay out of it.
SEA_OPEN = 3
# Open sea kept around the land when the board is cropped (game tiles).
CROP_MARGIN = 70
# Water this narrow (working pixels, each side) still joins two pieces of
# land into one landmass when deciding what a removed region leaves behind.
RIVER_BRIDGE = 2


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
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("world")
    ap.add_argument("out")
    ap.add_argument("--regions", help="comma-separated region keys to keep (default: all)")
    ap.add_argument("--drop", default="", help="comma-separated tile keys to leave out")
    args = ap.parse_args()
    data = build(args.world,
                 regions=args.regions.split(",") if args.regions else None,
                 drop_tiles=[k for k in args.drop.split(",") if k])
    with open(args.out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"{len(data['tiles'])} tiles, {len(data['edges'])} borders, "
          f"{data['width']}x{data['height']}, {len(data['decor'])} decorations, "
          f"{sum(len(v) for v in data['biomes'].values()) // 1024} KiB biomes")
