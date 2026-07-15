"""Organic landmass growth around the path (to "simulate land").

Instead of a uniform ring of tiles hugging the road, land is grown with a
distance-falloff probability plus a few rounds of cellular-automata smoothing.
That coalesces speckle into coherent continents with irregular coasts — bays,
capes, inlets — while always keeping the road itself (and its immediate core)
on land. Fully seed-deterministic.
"""
from __future__ import annotations

_DIRS = [(+1, 0), (+1, -1), (0, -1), (-1, 0), (-1, +1), (0, +1)]

QR = tuple[int, int]


def noise01(seed: int, q: int, r: int) -> float:
    """Deterministic hash noise in [0, 1) for a (seed, q, r)."""
    h = (q * 374761393 + r * 668265263 + seed * 2246822519) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    h ^= h >> 16
    return (h & 0xFFFFFF) / float(0x1000000)


def grow_land(sources: list[QR], play: set, seed: int, *, radius: int,
              core: int, density: float, smooth: int) -> set:
    """Return the set of land hexes grown out from `sources`.

    play     : set of (q,r) hexes allowed on the board (frame-clipped)
    radius   : how many rings out from a source land may reach
    core     : rings that are always land (the road + its banks)
    density  : 0..1 base coverage in the probabilistic outer rings
    smooth   : cellular-automata rounds (coherent coasts; higher = smoother)
    """
    # multi-source BFS distance from the sources, bounded by radius
    dist: dict[QR, int] = {}
    frontier: list[QR] = []
    for s in sources:
        if s in play and s not in dist:
            dist[s] = 0
            frontier.append(s)
    step = 0
    while frontier and step < radius:
        step += 1
        nxt = []
        for q, r in frontier:
            for dq, dr in _DIRS:
                nk = (q + dq, r + dr)
                if nk in play and nk not in dist:
                    dist[nk] = step
                    nxt.append(nk)
        frontier = nxt

    src = set(sources)
    span = max(1, radius - core)
    land: set = set()
    for k, d in dist.items():
        if d <= core:
            land.add(k)
        else:
            p = max(0.0, 1.0 - (d - core) / span)   # denser near the road
            if noise01(seed, k[0], k[1]) < density * p:
                land.add(k)

    universe = set(dist)
    for _ in range(smooth):                          # CA: coalesce + smooth
        new: set = set()
        for k in universe:
            q, r = k
            if k in src or dist[k] <= core:
                new.add(k)
                continue
            c = sum(1 for dq, dr in _DIRS if (q + dq, r + dr) in land)
            if c >= 4 or (k in land and c >= 3):
                new.add(k)
        land = new
    return land
