"""Hex-grid math (Red Blob Games conventions).

Axial coords (q, r); cube coords (q, r, s) with s = -q - r.
Supports pointy-top and flat-top orientations. Pure standard library.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT3 = math.sqrt(3.0)

# Axial neighbour directions, shared by both orientations.
AXIAL_DIRECTIONS = [(+1, 0), (+1, -1), (0, -1), (-1, 0), (-1, +1), (0, +1)]


@dataclass(frozen=True)
class Hex:
    """A hex in axial coordinates."""
    q: int
    r: int

    @property
    def s(self) -> int:
        return -self.q - self.r

    def __add__(self, other: "Hex") -> "Hex":
        return Hex(self.q + other.q, self.r + other.r)

    def neighbor(self, direction: int) -> "Hex":
        dq, dr = AXIAL_DIRECTIONS[direction % 6]
        return Hex(self.q + dq, self.r + dr)

    def neighbors(self) -> list["Hex"]:
        return [self.neighbor(i) for i in range(6)]


def distance(a: Hex, b: Hex) -> int:
    return (abs(a.q - b.q) + abs(a.r - b.r) + abs(a.s - b.s)) // 2


def ring(center: Hex, radius: int) -> list[Hex]:
    """All hexes exactly `radius` steps from center."""
    if radius <= 0:
        return [center]
    results = []
    h = center + Hex(AXIAL_DIRECTIONS[4][0] * radius, AXIAL_DIRECTIONS[4][1] * radius)
    for i in range(6):
        for _ in range(radius):
            results.append(h)
            h = h.neighbor(i)
    return results


def spiral(center: Hex, radius: int) -> list[Hex]:
    """center plus every hex within `radius` (a filled disc)."""
    out = [center]
    for k in range(1, radius + 1):
        out.extend(ring(center, k))
    return out


# ---------------------------------------------------------------------------
# Fractional-hex rounding & line drawing
# ---------------------------------------------------------------------------

def _cube_round(q: float, r: float, s: float) -> Hex:
    rq, rr, rs = round(q), round(r), round(s)
    dq, dr, ds = abs(rq - q), abs(rr - r), abs(rs - s)
    if dq > dr and dq > ds:
        rq = -rr - rs
    elif dr > ds:
        rr = -rq - rs
    else:
        rs = -rq - rr
    return Hex(int(rq), int(rr))


def line(a: Hex, b: Hex) -> list[Hex]:
    """Contiguous hex line from a to b (inclusive)."""
    n = distance(a, b)
    if n == 0:
        return [a]
    out = []
    for i in range(n + 1):
        t = i / n
        q = a.q + (b.q - a.q) * t
        r = a.r + (b.r - a.r) * t
        s = a.s + (b.s - a.s) * t
        out.append(_cube_round(q, r, s))
    return out


# ---------------------------------------------------------------------------
# Layout: hex <-> pixel
# ---------------------------------------------------------------------------

class Layout:
    """Converts hex coords to/from pixel positions.

    orientation: "pointy" (vertex up) or "flat" (edge up).
    size: hex "radius" (center to a corner) in pixels.
    """

    def __init__(self, size: float, origin: tuple[float, float] = (0.0, 0.0),
                 orientation: str = "pointy"):
        self.size = size
        self.origin = origin
        self.orientation = orientation

    def to_pixel(self, h: Hex) -> tuple[float, float]:
        ox, oy = self.origin
        if self.orientation == "pointy":
            x = self.size * (SQRT3 * h.q + SQRT3 / 2 * h.r)
            y = self.size * (1.5 * h.r)
        else:  # flat
            x = self.size * (1.5 * h.q)
            y = self.size * (SQRT3 / 2 * h.q + SQRT3 * h.r)
        return (x + ox, y + oy)

    def from_pixel(self, x: float, y: float) -> Hex:
        ox, oy = self.origin
        px, py = (x - ox) / self.size, (y - oy) / self.size
        if self.orientation == "pointy":
            q = SQRT3 / 3 * px - 1.0 / 3 * py
            r = 2.0 / 3 * py
        else:
            q = 2.0 / 3 * px
            r = -1.0 / 3 * px + SQRT3 / 3 * py
        return _cube_round(q, r, -q - r)

    def corners(self, h: Hex) -> list[tuple[float, float]]:
        cx, cy = self.to_pixel(h)
        pts = []
        start = 30.0 if self.orientation == "pointy" else 0.0
        for i in range(6):
            ang = math.radians(start + 60 * i)
            pts.append((cx + self.size * math.cos(ang),
                        cy + self.size * math.sin(ang)))
        return pts

    def hex_dimensions(self) -> tuple[float, float]:
        """(width, height) of a single hex's bounding box."""
        if self.orientation == "pointy":
            return (SQRT3 * self.size, 2 * self.size)
        return (2 * self.size, SQRT3 * self.size)


def offset_to_axial(row: int, col: int, orientation: str = "pointy") -> Hex:
    """odd-r (pointy) / odd-q (flat) offset -> axial."""
    if orientation == "pointy":  # odd-r
        q = col - (row - (row & 1)) // 2
        return Hex(q, row)
    else:  # odd-q
        r = row - (col - (col & 1)) // 2
        return Hex(col, r)


def rectangle(rows: int, cols: int, orientation: str = "pointy") -> list[Hex]:
    """A rectangular block of hexes in offset layout, returned as axial."""
    return [offset_to_axial(row, col, orientation)
            for row in range(rows) for col in range(cols)]
