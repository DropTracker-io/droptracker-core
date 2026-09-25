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
    return np.isin(lab, 1 + np.nonzero(sizes >= 110)[0])


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
    for (x0, x1, y0, y1), swap in OVERRIDES:
        c0, r1 = to_grid(x0, y0)
        c1, r0 = to_grid(x1, y1)
        box = np.zeros_like(land)
        box[max(int(r0), 0):int(r1), max(int(c0), 0):int(c1)] = True
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
              min_len: int = 10) -> str:
    """Smooth outline(s) of a boolean mask as one SVG path (evenodd holes)."""
    f = ndi.gaussian_filter(np.pad(mask.astype(float), 2), sigma)
    parts = []
    for c in measure.find_contours(f, 0.5):
        if len(c) < min_len:
            continue
        c = measure.approximate_polygon(c, tol)
        if len(c) < 4:
            continue
        c = _chaikin(c[:-1] if np.allclose(c[0], c[-1]) else c)
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
    label = np.full((h, w), -1)
    for rk in region_keys:
        idx = [i for i, m in enumerate(meta) if m["region"] == rk]
        if not idx:
            continue
        if rk == "abyss":
            ground = reach & disk((h, w), ax, ay, ar + 3)
        else:
            ground = reach & (area_w == region_keys.index(rk)) & ~disk((h, w), ax, ay, ar + 6)
        d = np.stack([np.hypot(XX - seeds[i][0], YY - seeds[i][1]) for i in idx])
        near = np.array(idx)[d.argmin(0)]
        label[ground] = near[ground]
    # Drop specks: bits of a territory too small to matter go neutral.
    for i in range(len(seeds)):
        m = label == i
        lab, n = ndi.label(m)
        if n > 1:
            sizes = ndi.sum(m, lab, range(1, n + 1))
            label[m & np.isin(lab, 1 + np.nonzero(sizes < 40)[0])] = -1

    # Badge anchors: the boss's true spot, nudged inward if it sits on an edge.
    anchors = []
    for i, (cx, cy) in enumerate(seeds):
        cell = (label == i) & land
        inside = ndi.distance_transform_edt(cell)
        near = disk(cell.shape, cx, cy, 16 / G * 2)
        score = np.minimum(inside, 22 / G) - 0.08 * np.hypot(xx - cx, yy - cy)
        score[~near | ~cell] = -1e9
        r, c = np.unravel_index(score.argmax(), score.shape)
        anchors.append((float(c * G), float(r * G)))
    labels = [geo.label_for(m["key"]) for m in meta]
    halfw = [max(len(t) * 4.2 + 17, BADGE_R + 4) for t in labels]
    anchors = _relax(anchors, seeds, [(label == i) & land for i in range(len(seeds))], halfw)

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
        tiles.append({**m, "label": labels[i], "path": mask_path(cell, sigma=1.2),
                      "x": round(anchors[i][0], 1), "y": round(anchors[i][1], 1),
                      "true": [round(float(seeds[i][0]) * G, 1), round(float(seeds[i][1]) * G, 1)],
                      "area": area})

    regions, placed = [], []
    for region in geo.REGIONS:
        idx = [i for i, m in enumerate(meta) if m["region"] == region["key"]]
        union = np.isin(label, idx)
        spot = None if region.get("inset") else _region_label_spot(
            union & land, region["name"], anchors, halfw, placed)
        regions.append({"key": region["key"], "name": region["name"], "color": region["color"],
                        "path": mask_path(union, sigma=1.2), "label": spot})
    # Land in play vs scenery nobody can claim.
    neutral = land & (label < 0)

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
        "width": e["x1"] - e["x0"], "height": e["y1"] - e["y0"], "extent": e,
        "land": mask_path(land, sigma=1.0, tol=0.4),
        "neutral": mask_path(neutral, sigma=1.2), "biomes": biome_paths,
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


# Space a boss medallion and its name scroll take up around its anchor
# (board units): the medallion's radius, then BADGE_UP above the anchor and
# BADGE_DOWN below (the scroll hangs underneath). Width comes from the name.
BADGE_R, BADGE_UP, BADGE_DOWN = 30, 34, 58


def _relax(anchors, seeds, cells, halfw, iters=300):
    """Push overlapping medallions apart, keeping each on its own territory
    and within 90 tiles of where the boss really is."""
    P = np.array(anchors, float)
    T = np.array(seeds, float) * G
    snap = []
    for cell in cells:
        core = ndi.binary_erosion(cell, iterations=4)
        if not core.any():
            core = cell
        _, (ri, ci) = ndi.distance_transform_edt(~core, return_indices=True)
        snap.append((core, ri, ci))
    pad = 4
    for _ in range(iters):
        moved = False
        for i in range(len(P)):
            for j in range(i + 1, len(P)):
                dx, dy = P[j, 0] - P[i, 0], P[j, 1] - P[i, 1]
                ox = halfw[i] + halfw[j] + pad - abs(dx)
                if ox <= 0:
                    continue
                oy = (min(P[i, 1], P[j, 1]) + BADGE_DOWN) - (max(P[i, 1], P[j, 1]) - BADGE_UP) + pad
                if oy <= 0:
                    continue
                moved = True
                if ox < oy:
                    step = (ox / 2 + .5) * (1 if dx >= 0 else -1)
                    P[i, 0] -= step
                    P[j, 0] += step
                else:
                    step = (oy / 2 + .5) * (1 if dy >= 0 else -1)
                    P[i, 1] -= step
                    P[j, 1] += step
        for i in range(len(P)):
            v = P[i] - T[i]
            d = math.hypot(*v)
            if d > 90:
                P[i] = T[i] + v / d * 90
            core, ri, ci = snap[i]
            r = int(np.clip(P[i, 1] / G, 0, core.shape[0] - 1))
            c = int(np.clip(P[i, 0] / G, 0, core.shape[1] - 1))
            if not core[r, c]:
                P[i] = (ci[r, c] * G, ri[r, c] * G)
        if not moved:
            break
    return [(round(float(x), 1), round(float(y), 1)) for x, y in P]


def _region_label_spot(mask, name, anchors, halfw, placed):
    """Where a region's name goes: inside the region, clear of every
    medallion and every name already placed, as central as that allows."""
    if not mask.any():
        return None
    core = ndi.binary_erosion(mask, iterations=6)
    home = core if core.any() else mask
    cy, cx = ndi.center_of_mass(home)
    cx, cy = cx * G, cy * G
    # A small or crowded region may have no clear spot inside it, so the
    # name may also sit just outside (over the sea or a neighbour), at a cost.
    mask = ndi.binary_dilation(mask, iterations=22)
    half = len(name) * 9.5 + 10
    boxes = [(x - hw, x + hw, y - BADGE_UP, y + BADGE_DOWN) for (x, y), hw in zip(anchors, halfw)]
    boxes = np.array(boxes + placed, float)
    ys, xs = np.nonzero(mask[::2, ::2])
    best, spot = -1e18, (cx, cy)
    for r, c in zip(ys * 2, xs * 2):
        x, y = c * G, r * G  # text baseline centre; glyphs span y-30..y+6
        dx = np.maximum(np.maximum(boxes[:, 0] - (x + half), (x - half) - boxes[:, 1]), 0)
        dy = np.maximum(np.maximum(boxes[:, 2] - (y + 8), (y - 34) - boxes[:, 3]), 0)
        clear = float(np.min(np.hypot(dx, dy)))
        score = ((0 if clear > 6 else -5000) + min(clear, 40) - 0.35 * math.hypot(x - cx, y - cy)
                 - (0 if home[r, c] else 45))
        if score > best:
            best, spot = score, (x, y)
    placed.append((spot[0] - half, spot[0] + half, spot[1] - 34, spot[1] + 8))
    return [round(float(spot[0]), 1), round(float(spot[1]), 1)]


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
            half = (len(reg["name"]) * 9.5 + 16) / G
            keep_clear[int(max(ly - 40 / G, 0)):int(ly + 12 / G),
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
