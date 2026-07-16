"""Procedural path generation — self-avoiding walk with a minimum gap.

The path visits region-seed waypoints in order. Between waypoints it grows one
hex at a time toward the target (strong greedy bias + mild lateral wiggle) using
depth-first search with a bounded node budget. A hard constraint keeps the road
unambiguous: consecutive tiles are edge-adjacent (the road), but every
*non*-consecutive tile stays at least `min_gap` hexes away — so the path never
touches itself and the direction of travel is always clear.

Hot-path hexes are plain (q, r) tuples and the play-area is a precomputed set,
so a whole board generates in a few milliseconds.
"""
from __future__ import annotations

import random

from .hexgrid import Hex, Layout

QR = tuple[int, int]
_DIRS = [(+1, 0), (+1, -1), (0, -1), (-1, 0), (-1, +1), (0, +1)]


def _neighbors(h: QR) -> list[QR]:
    q, r = h
    return [(q + dq, r + dr) for dq, dr in _DIRS]


def _dist(a: QR, b: QR) -> int:
    dq = a[0] - b[0]
    dr = a[1] - b[1]
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def _valid(c: QR, head: QR, occupied: set, min_gap: int, allowed: set) -> bool:
    if c in occupied or c not in allowed:
        return False
    if min_gap <= 1:
        return True
    if min_gap == 2:
        q, r = c
        for dq, dr in _DIRS:
            n = (q + dq, r + dr)
            if n != head and n in occupied:
                return False
        return True
    for p in occupied:                         # general case (min_gap > 2)
        if p != head and _dist(c, p) < min_gap:
            return False
    return True


def _order(head: QR, goal: QR, rng: random.Random, spread: float) -> list[QR]:
    """Neighbours of head, best (closest-to-goal + small noise) placed LAST."""
    scored = [(_dist(n, goal) + rng.random() * spread, n) for n in _neighbors(head)]
    scored.sort(reverse=True)
    return [n for _, n in scored]


def _leg(start: QR, goal: QR, occupied: set, rng: random.Random, *,
         min_gap: int, spread: float, allowed: set, arrive: int,
         budget: int) -> list[QR] | None:
    """Self-avoiding walk start->goal (within `arrive`). start is in occupied.

    Returns [start, ..., end] or None. Rolls back its own additions on failure.
    """
    path = [start]
    stack = [_order(start, goal, rng, spread)]
    added: list[QR] = []
    nodes = 0
    while stack:
        nodes += 1
        if nodes > budget:
            break
        head = path[-1]
        if head != start and _dist(head, goal) <= arrive:
            return path
        cands = stack[-1]
        placed = False
        while cands:
            c = cands.pop()
            if _valid(c, head, occupied, min_gap, allowed):
                path.append(c)
                occupied.add(c)
                added.append(c)
                stack.append(_order(c, goal, rng, spread))
                placed = True
                break
        if not placed:
            stack.pop()
            dead = path.pop()
            if dead != start:
                occupied.discard(dead)
                if added and added[-1] == dead:
                    added.pop()
            if not path:
                break
    for h in added:                            # failure: undo
        occupied.discard(h)
    return None


def _open_score(c: QR, occupied: set, min_gap: int, allowed: set) -> int:
    """How many of `c`'s neighbours are still walkable (roughly, how much room
    lies beyond it). Used to steer the length-fill wander away from dead ends."""
    q, r = c
    n = 0
    for dq, dr in _DIRS:
        nb = (q + dq, r + dr)
        if nb in allowed and nb not in occupied:
            n += 1
    return n


def _extend_tail(full: list[QR], occupied: set, rng: random.Random, *,
                 need: int, min_gap: int, allowed: set, budget: int) -> int:
    """Grow the tail of `full` by up to `need` self-avoiding tiles.

    A DFS wander from the current finish: at each step it prefers the neighbour
    that leaves the most room (openness-greedy) so it fills space without
    trapping itself, backtracking only into its own additions. If it can't reach
    the full `need` within budget it keeps the longest reach found (never rolls
    back to zero). Returns the number of tiles actually added.
    """
    if need <= 0:
        return 0

    def order(head: QR) -> list[QR]:
        cands = [c for c in _neighbors(head)
                 if _valid(c, head, occupied, min_gap, allowed)]
        # best (most open, small noise) placed LAST so it is popped first.
        cands.sort(key=lambda c: _open_score(c, occupied, min_gap, allowed)
                   + rng.random())
        return cands

    base = len(full)
    best: list[QR] = []                          # longest tail reached so far
    stack = [order(full[-1])]
    nodes = 0
    while stack:
        nodes += 1
        if nodes > budget or len(full) - base >= need:
            break
        cands = stack[-1]
        placed = False
        while cands:
            c = cands.pop()
            if _valid(c, full[-1], occupied, min_gap, allowed):
                full.append(c)
                occupied.add(c)
                stack.append(order(c))
                placed = True
                if len(full) - base > len(best):
                    best = full[base:]
                break
        if not placed:
            stack.pop()
            if len(full) > base:                 # backtrack only our own tiles
                occupied.discard(full.pop())
            else:
                break
    reach = min(need, len(best))                 # settle on the best reach
    while len(full) > base:
        occupied.discard(full.pop())
    for c in best[:reach]:
        full.append(c)
        occupied.add(c)
    return reach


def build_path(layout: Layout, waypoints: list[Hex], rng: random.Random, *,
               allowed: set, hard: set | None = None, min_gap: int = 2,
               meander: float = 0.55, budget: int = 9000,
               target_len: int | None = None
               ) -> tuple[list[Hex], int]:
    """Contiguous, self-avoiding path visiting waypoints.

    `hard` is the set of (q,r) waypoints that matter (region seeds); soft ones
    (sub-waypoints that only add wiggle) are skipped silently when unreachable.
    Returns (path, hard_skipped). The min-gap guarantee is never relaxed: an
    unreachable waypoint is skipped rather than force-touched.

    When `target_len` is given the path is forced to *exactly* that many tiles:
    the natural waypoint path (which tends to undershoot) is padded out with an
    openness-greedy self-avoiding wander off its finish, or truncated on the
    rare overshoot. The min-gap guarantee still holds for the padded tail.
    """
    hard = hard if hard is not None else set()
    clean: list[QR] = []
    for w in waypoints:
        qr = (w.q, w.r)
        if not clean or qr != clean[-1]:
            clean.append(qr)
    if not clean:
        return [], 0

    spread = 0.35 + meander * 1.5
    occupied: set = {clean[0]}
    full: list[QR] = [clean[0]]
    skipped = 0
    for goal in clean[1:]:
        if goal in occupied:
            continue
        leg = None
        # progressively straighter + looser arrival; a near-beeline still
        # honours min_gap but almost never self-traps, so skips stay rare.
        for arrive, sf in ((1, 1.0), (1, 0.35), (2, 0.12), (3, 0.0)):
            leg = _leg(full[-1], goal, occupied, rng, min_gap=min_gap,
                       spread=spread * sf, allowed=allowed, arrive=arrive,
                       budget=budget)
            if leg is not None:
                break
        if leg is None:                        # unreachable cleanly -> skip it
            if goal in hard:
                skipped += 1
            continue
        full.extend(leg[1:])

    if target_len is not None and target_len >= 1:
        if len(full) > target_len:             # rare overshoot -> trim the tail
            trimmed = set(full[target_len:])
            occupied -= trimmed
            del full[target_len:]
        elif len(full) < target_len:           # usual case -> pad to exact count
            need = target_len - len(full)
            got = _extend_tail(full, occupied, rng, need=need,
                               min_gap=min_gap, allowed=allowed,
                               budget=budget * 4)
            if got < need:                     # finish boxed in -> pad the start
                full.reverse()                 # tail is now the old start
                got += _extend_tail(full, occupied, rng, need=need - got,
                                    min_gap=min_gap, allowed=allowed,
                                    budget=budget * 4)
                full.reverse()

    return [Hex(q, r) for q, r in full], skipped
