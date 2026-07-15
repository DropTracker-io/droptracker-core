"""Board data model + procedural generation.

Board.generate() produces a fully-populated, seedable board:
  * a randomized region layout (start corner, snake axis, rows, jitter, region
    subset + biome shuffle) so every seed looks distinct;
  * a long, richly-winding self-avoiding start->finish path (per-leg
    sub-waypoints add wiggle, min-gap keeps direction unambiguous);
  * organic landmasses grown around the road (terrain.grow_land) to simulate
    land, with irregular coasts;
  * region (biome) assignment via nearest-seed Voronoi, tile roles, decorations.
A hard frame margin keeps every tile clear of the image edge. render.py turns
this data into layered SVG; nothing here knows about SVG.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import biomes, hexgrid, pathgen, terrain
from .hexgrid import Hex, Layout, SQRT3


@dataclass
class Tile:
    hex: Hex
    x: float
    y: float
    biome: str
    is_path: bool = False
    path_index: int = -1          # order along the path, -1 if off-path
    role: str = "field"           # field | path | start | finish
    decor: str | None = None
    glow: str | None = None       # a colour key from biomes.GLOW_COLORS


@dataclass
class Region:
    key: str
    name: str
    seed: Hex
    x: float
    y: float


@dataclass
class Board:
    width: float
    height: float
    size: float
    orientation: str
    style: str
    seed: int
    layout: Layout
    margin: float = 0.0
    labels: bool = True
    skipped: int = 0              # region waypoints the path couldn't reach cleanly
    tiles: dict[tuple[int, int], Tile] = field(default_factory=dict)
    path: list[Hex] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    title: str = "Gielinor Journeys"
    subtitle: str = "a procedurally generated board"

    # ------------------------------------------------------------------
    @classmethod
    def generate(cls, *, width=1600, height=900, size=34, orientation="pointy",
                 style="path", seed=None, tour=None, regions=None, min_gap=2,
                 meander=0.55, margin=None, core=1, land=None,
                 land_density=None, smooth=2, labels=True,
                 title="Gielinor Journeys",
                 subtitle="a procedurally generated board") -> "Board":
        if seed is None:
            seed = random.randrange(1, 2**31)
        rng = random.Random(seed)
        layout = Layout(size, origin=(size * 1.5, size * 1.4),
                        orientation=orientation)

        # -- frame geometry ---------------------------------------------
        if margin is None:
            margin = round(max(size * 2.0, 0.045 * min(width, height)))
        hw, hh = layout.hex_dimensions()
        tx0, tx1 = margin + hw / 2, width - margin - hw / 2
        ty0, ty1 = margin + hh / 2, height - margin - hh / 2
        band = 1.3 * SQRT3 * size                 # keep the road off the frame
        px0, px1 = tx0 + band, tx1 - band
        py0, py1 = ty0 + band, ty1 - band

        def in_play(h: Hex) -> bool:
            x, y = layout.to_pixel(h)
            return tx0 <= x <= tx1 and ty0 <= y <= ty1

        def in_path_area(h: Hex) -> bool:
            x, y = layout.to_pixel(h)
            return px0 <= x <= px1 and py0 <= y <= py1

        board = cls(width=width, height=height, size=size,
                    orientation=orientation, style=style, seed=seed,
                    layout=layout, margin=margin, labels=labels,
                    title=title, subtitle=subtitle)

        full_universe = board._universe()
        play_set = {(h.q, h.r) for h in full_universe if in_play(h)}
        area_hexes = [h for h in full_universe if in_path_area(h)]
        path_area = {(h.q, h.r) for h in area_hexes}

        regions = board._place_regions(tour, regions, rng,
                                       (px0, py0, px1, py1), area_hexes)
        board.regions = regions

        def nearest_region(h: Hex) -> Region:
            hx, hy = layout.to_pixel(h)
            return min(regions, key=lambda r: (r.x - hx) ** 2 + (r.y - hy) ** 2)

        # --- long self-avoiding path -----------------------------------
        amp = size * (0.7 + meander * 2.6)
        seeds_only = [r.seed for r in regions]
        hard = {(r.seed.q, r.seed.r) for r in regions}
        # Try a richly-winding path; if any *region* becomes unreachable, step
        # down the wiggle (full -> half -> straight spine). The spine always
        # reaches every region, so region-skips are driven to zero.
        path, board.skipped = [], 99
        for scale in (1.0, 0.55, 0.0):
            wps = (seeds_only if scale == 0.0 else
                   _enrich(layout, regions, rng, path_area, amp * scale, size * 3.4))
            path, board.skipped = pathgen.build_path(
                layout, wps, rng, allowed=path_area, hard=hard,
                min_gap=min_gap, meander=meander)
            if board.skipped == 0:
                break
        board.path = path                     # always Tutorial -> Varlamore

        # --- organic landmasses around the road ------------------------
        if land is None:
            land = 3 if style == "path" else 6
        if land_density is None:
            land_density = 0.55 if style == "path" else 0.9
        path_qr = [(h.q, h.r) for h in path]
        sources = list(path_qr)
        if style == "filled":
            sources += [(r.seed.q, r.seed.r) for r in regions]
        land_set = terrain.grow_land(sources, play_set, seed, radius=land,
                                     core=core, density=land_density,
                                     smooth=smooth)
        land_set |= set(path_qr) & play_set

        # --- build tiles ------------------------------------------------
        for (q, r) in land_set:
            h = Hex(q, r)
            px, py = layout.to_pixel(h)
            board.tiles[(q, r)] = Tile(hex=h, x=px, y=py,
                                       biome=nearest_region(h).key)
        for i, h in enumerate(path):
            t = board.tiles.get((h.q, h.r))
            if t is None:
                px, py = layout.to_pixel(h)
                t = Tile(hex=h, x=px, y=py, biome=nearest_region(h).key)
                board.tiles[(h.q, h.r)] = t
            t.is_path = True
            t.path_index = i
            t.role = "path"

        board._assign_roles()
        board._decorate(rng)
        return board

    # ------------------------------------------------------------------
    def _universe(self) -> list[Hex]:
        size, W, H = self.size, self.width, self.height
        v_step = 1.5 * size if self.orientation == "pointy" else SQRT3 * size
        h_step = SQRT3 * size if self.orientation == "pointy" else 1.5 * size
        r_rows = int(H / v_step) + 3
        c_cols = int(W / h_step) + 3
        return [hexgrid.offset_to_axial(row, col, self.orientation)
                for row in range(r_rows) for col in range(c_cols)]

    def _place_regions(self, tour: list[str] | None, count: int | None,
                       rng: random.Random,
                       area: tuple[float, float, float, float],
                       area_hexes: list[Hex]) -> list[Region]:
        """Randomized serpentine seed layout snapped to real path-area hexes.

        With tour=None the region *count* is configurable (`count`, default 8)
        but the tour always opens on "tutorial" and closes on "varlamore";
        the regions in between are a random sample of the rest, kept in
        `biomes.DEFAULT_TOUR` order so the route reads consistently board to
        board. The snake's axis/orientation/rows and per-slot jitter still
        randomize so the shape/spread differs each seed. Pass an explicit
        `tour` for full manual control over the region order.
        """
        x0, y0, x1, y1 = area
        pix = [(h, *self.layout.to_pixel(h)) for h in area_hexes]

        if tour is None:
            total = max(2, min(count or 8, len(biomes.DEFAULT_TOUR)))
            middle_pool = [k for k in biomes.DEFAULT_TOUR
                          if k not in ("tutorial", "varlamore")]
            middle = set(rng.sample(middle_pool, min(total - 2, len(middle_pool))))
            chosen = (["tutorial"] +
                     [k for k in middle_pool if k in middle] +
                     ["varlamore"])
            k = len(chosen)
            axis = rng.choice(("h", "v"))
            rows = rng.choice((2, 3)) if k >= 9 else 2
            flipx, flipy = rng.random() < 0.5, rng.random() < 0.5
        else:
            chosen = [t for t in tour if t in biomes.BIOMES]
            k = len(chosen)
            axis, rows, flipx, flipy = "h", (2 if k > 4 else 1), False, False
        cols = max(1, math.ceil(k / rows))

        # Visit slots in serpentine (boustrophedon) grid order: non-crossing by
        # construction, so the spine always routes. Moderate jitter keeps that
        # property while still varying every board.
        order: list[tuple[int, int]] = []
        if axis == "h":
            for r in range(rows):
                cs = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
                order += [(c, r) for c in cs]
        else:
            for c in range(cols):
                rs = range(rows) if c % 2 == 0 else range(rows - 1, -1, -1)
                order += [(c, r) for r in rs]
        order = order[:k]

        cw = (x1 - x0) / max(1, cols - 1) if cols > 1 else 0
        ch = (y1 - y0) / max(1, rows - 1) if rows > 1 else 0
        sep = 3
        regions: list[Region] = []
        used: list[Hex] = []
        for (c, r), key in zip(order, chosen):
            cc = (cols - 1 - c) if flipx else c
            rr = (rows - 1 - r) if flipy else r
            cx = x0 + cc * cw + rng.uniform(-0.24, 0.24) * (cw or (x1 - x0) or 1)
            cy = y0 + rr * ch + rng.uniform(-0.22, 0.22) * (ch or (y1 - y0) or 1)
            cx = min(max(cx, x0), x1)
            cy = min(max(cy, y0), y1)
            cand = sorted(pix, key=lambda t: (t[1] - cx) ** 2 + (t[2] - cy) ** 2)
            pick = next(((h, px, py) for h, px, py in cand
                         if all(hexgrid.distance(h, u) >= sep for u in used)), None)
            if pick is None:
                pick = next(((h, px, py) for h, px, py in cand
                             if all((h.q, h.r) != (u.q, u.r) for u in used)),
                            cand[0])
            seed_hex, sx, sy = pick
            used.append(seed_hex)
            b = biomes.biome(key)
            regions.append(Region(key=key, name=b.name, seed=seed_hex, x=sx, y=sy))
        return regions

    def _assign_roles(self) -> None:
        # Only start/finish are ever marked — every other path tile stays a
        # plain, identically-coloured stepping stone (no random highlights).
        if not self.path:
            return
        start, finish = self.path[0], self.path[-1]
        self.tiles[(start.q, start.r)].role = "start"
        self.tiles[(start.q, start.r)].glow = "start"
        self.tiles[(finish.q, finish.r)].role = "finish"
        self.tiles[(finish.q, finish.r)].glow = "finish"

    def _decorate(self, rng: random.Random) -> None:
        # Decorations never sit on active path tiles (road/start/finish/event)
        # — only off-road field tiles get icons, so the road stays unambiguous.
        for t in self.tiles.values():
            if t.is_path:
                continue
            b = biomes.biome(t.biome)
            if rng.random() < b.decor_density and b.decor:
                t.decor = _weighted(b.decor, rng)

    # ------------------------------------------------------------------
    def region_label_points(self) -> list[tuple[Region, float, float]]:
        """Centroid of each region's own tiles (falls back to seed)."""
        acc: dict[str, list[float]] = {}
        for t in self.tiles.values():
            a = acc.setdefault(t.biome, [0.0, 0.0, 0.0])
            a[0] += t.x
            a[1] += t.y
            a[2] += 1
        out = []
        for r in self.regions:
            a = acc.get(r.key)
            if a and a[2] > 0:
                out.append((r, a[0] / a[2], a[1] / a[2]))
            else:
                out.append((r, r.x, r.y))
        return out


def _enrich(layout: Layout, regions: list[Region], rng: random.Random,
            path_area: set, amp: float, spacing: float) -> list[Hex]:
    """Region seeds plus per-leg sub-waypoints (perpendicular sine offsets).

    Sub-waypoints stay inside their own corridor, so they lengthen and wind the
    road without colliding with other legs. Anything outside the path area is
    dropped, so the min-gap guarantee is unaffected.
    """
    seeds = [r.seed for r in regions]
    if not seeds:
        return []
    px = [layout.to_pixel(s) for s in seeds]
    out: list[Hex] = [seeds[0]]
    for idx in range(len(seeds) - 1):
        ax, ay = px[idx]
        bx, by = px[idx + 1]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        mx, my = (ax + bx) / 2, (ay + by) / 2
        # clamp wiggle to local corridor: never bulge toward another region
        near = min((math.hypot(qx - mx, qy - my)
                    for j, (qx, qy) in enumerate(px) if j not in (idx, idx + 1)),
                   default=length)
        amp_leg = min(amp, 0.34 * near, 0.4 * length)
        n = max(0, int(length / spacing) - 1)
        if n > 0 and length > 1e-6 and amp_leg > layout.size * 0.4:
            nx, ny = -dy / length, dx / length
            phase = rng.uniform(0, 2 * math.pi)
            freq = rng.uniform(0.6, 1.5)
            sign = rng.choice((-1, 1))
            for i in range(1, n + 1):
                t = i / (n + 1)
                env = math.sin(math.pi * t)
                off = sign * amp_leg * math.sin(2 * math.pi * freq * t + phase) * env
                h = layout.from_pixel(ax + dx * t + nx * off,
                                      ay + dy * t + ny * off)
                if (h.q, h.r) in path_area:
                    out.append(h)
        out.append(seeds[idx + 1])
    return out


def _weighted(table: list[tuple[str, float]], rng: random.Random) -> str:
    total = sum(w for _, w in table)
    pick = rng.uniform(0, total)
    upto = 0.0
    for name, w in table:
        upto += w
        if pick <= upto:
            return name
    return table[-1][0]
