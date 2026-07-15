"""Procedural board-game board generation (web46a).

Bridges the vendored ``services.boardgen`` engine (a zero-dependency, layered-SVG
OSRS-style hex-board generator) to the board-game event system so an admin can
roll a whole playable board — art *and* the sequential tile track — in one click
instead of hand-placing every tile.

The mapping is direct because both sides model a board the same way:

  * boardgen's ``board.path`` is an ordered start->finish list of hexes; each has
    ``path_index`` (0..N-1, contiguous by construction) and pixel coords
    ``tile.x``/``tile.y`` in the SVG's ``viewBox="0 0 width height"`` space.
  * an ``EventBoardTile`` is exactly that: ``idx`` (0..N-1 advancement order) at a
    fractional ``x``/``y`` on the background image, plus a difficulty/kind.

So ``board.path[i]`` -> ``EventBoardTile(idx=i, x=tile.x/W, y=tile.y/H, ...)`` with
the difficulty cycling air->water->earth->fire (the designer's convention) and
the first/last tiles marked start/finish. The rendered SVG becomes the tile
background; the off-road field tiles, biome fills and region banners are art
only. Path tiles render as plain stones, so the event's rune/outline tile
overlay sits cleanly on top.

Two outputs:
  * ``build_board_assets`` — the pure, offline part (no I/O): Board -> (svg, tiles,
    width, height, meta). Unit-testable without B2 or chromium.
  * ``upload_board_svg`` / ``rasterize_svg_to_png`` — the I/O edges: publish the SVG
    to B2 (served as the ``<img>`` background) and, on demand, flatten it to a PNG
    for a Discord attachment or a download.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass

from services.boardgen import Board, render

# Mirrors db.EVENT_TASK_DIFFICULTIES (db/models/events.py) — kept local so the
# pure generation half imports nothing heavy (SQLAlchemy) and stays unit-
# testable in isolation. Order is the designer's air->water->earth->fire cycle.
EVENT_TASK_DIFFICULTIES = ("air", "water", "earth", "fire")

# Board sizing: boardgen's 1600x900 base canvas fits ~54 path tiles at the
# default region count; a larger --tiles target scales the canvas area
# proportionally (mirrors generate.py so results match the CLI).
_BASE_W, _BASE_H = 1600.0, 900.0
_BASE_TILES = 54.0

# Guardrails on the admin-supplied knobs.
MIN_REGIONS, MAX_REGIONS = 2, 11
MIN_TILES, MAX_TILES = 10, 400          # 400 < the board's 512 EventBoardTile cap
STYLES = ("path", "filled")
_MAX_SEED = 2**31 - 1

# Chromium (already on the box) is the only available SVG rasterizer — no
# cairosvg / rsvg / inkscape. Used off the request thread, design-time only.
_CHROMIUM = os.getenv("CHROMIUM_BIN", "/usr/bin/chromium")


@dataclass
class GenParams:
    seed: int
    regions: int
    tiles: int
    style: str
    title: str
    subtitle: str
    watermark: str | None


def normalize_params(
    *,
    seed=None,
    regions=None,
    tiles=None,
    style=None,
    title=None,
    subtitle=None,
    watermark=None,
) -> GenParams:
    """Coerce/clamp the raw request knobs into a valid GenParams. Raises
    ValueError on a hard type problem; clamps everything else into range so a
    fat-fingered tile count can never break generation."""
    import random

    if seed is None:
        seed = random.randrange(1, _MAX_SEED)
    else:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise ValueError("seed must be an integer")
        # Keep it a stable, reproducible non-negative handle.
        seed = abs(seed) % _MAX_SEED or 1

    regions = _clamp_int(regions, MIN_REGIONS, MAX_REGIONS, default=8, name="regions")
    tiles = _clamp_int(tiles, MIN_TILES, MAX_TILES, default=60, name="tiles")

    style = (style or "path").strip().lower()
    if style not in STYLES:
        style = "path"

    title = (str(title).strip() if title else "") or "Gielinor Race"
    subtitle = (str(subtitle).strip() if subtitle else "") or "a DropTracker Board Game"
    watermark = (str(watermark).strip() or None) if watermark else None
    return GenParams(seed, regions, tiles, style, title[:120], subtitle[:120],
                     watermark[:60] if watermark else None)


def _clamp_int(value, lo, hi, *, default, name):
    if value is None:
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    return max(lo, min(hi, v))


def _canvas_for(tiles: int) -> tuple[int, int]:
    factor = (tiles / _BASE_TILES) ** 0.5
    return round(_BASE_W * factor), round(_BASE_H * factor)


def _difficulty_for(i: int) -> str:
    """air -> water -> earth -> fire, repeating (the designer's cycle)."""
    return EVENT_TASK_DIFFICULTIES[i % len(EVENT_TASK_DIFFICULTIES)]


def build_board(p: GenParams) -> Board:
    """Generate the boardgen Board for these params (pure, seedable)."""
    width, height = _canvas_for(p.tiles)
    return Board.generate(
        width=width, height=height, seed=p.seed, style=p.style,
        regions=p.regions, title=p.title, subtitle=p.subtitle,
    )


def board_to_tiles(board: Board) -> list[dict]:
    """Walk ``board.path`` into a BoardInput ``tiles`` array.

    idx follows the path order (0..N-1, contiguous), x/y are the tile's pixel
    centre as a fraction of the canvas, the ends are start/finish and every
    tile takes a cycled difficulty (admins can retune any tile afterward)."""
    w = float(board.width) or 1.0
    h = float(board.height) or 1.0
    last = len(board.path) - 1
    tiles: list[dict] = []
    for i, hex_ in enumerate(board.path):
        t = board.tiles.get((hex_.q, hex_.r))
        if t is None:                       # never happens (path tiles exist), be safe
            continue
        kind = "start" if i == 0 else "finish" if i == last else "normal"
        tiles.append({
            "idx": i,
            "x": round(min(1.0, max(0.0, t.x / w)), 4),
            "y": round(min(1.0, max(0.0, t.y / h)), 4),
            "difficulty": _difficulty_for(i),
            "tile_kind": kind,
        })
    return tiles


def build_board_assets(p: GenParams) -> dict:
    """The offline half: Board -> {svg, tiles, width, height, meta}. No I/O, so
    a unit test can exercise the whole mapping without B2 or chromium."""
    board = build_board(p)
    svg = render(board, watermark=p.watermark).render()
    tiles = board_to_tiles(board)
    return {
        "svg": svg,
        "tiles": tiles,
        "width": int(board.width),
        "height": int(board.height),
        "meta": {
            "seed": p.seed,
            "style": p.style,
            "regions": p.regions,
            "path_tiles": len(board.path),
            "total_tiles": len(board.tiles),
            "skipped_regions": board.skipped,
        },
    }


async def upload_board_svg(svg: str, event_id: int, seed: int) -> str:
    """Publish the generated SVG to B2 and return its public CDN URL (served as
    the tile-overlay background). Mirrors the board-background upload path."""
    from utils.b2_storage import upload_bytes
    from web_api.routes.submissions import B2_CDN_BASE_URL

    key = f"dt_uploads/boards/{event_id}-gen-{seed}-{uuid.uuid4().hex[:8]}.svg"
    await upload_bytes(svg.encode("utf-8"), key, "image/svg+xml")
    return f"{B2_CDN_BASE_URL.rstrip('/')}/{key}"


def _rasterize_sync(svg: str, width: int, height: int, *, scale: float) -> bytes:
    """Flatten an SVG to a PNG with headless chromium (the only rasterizer on
    the box). Design-time / on-demand only — never on a hot path."""
    w = max(1, round(width * scale))
    h = max(1, round(height * scale))
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "board.svg")
        out = os.path.join(tmp, "board.png")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(svg)
        cmd = [
            _CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu",
            "--hide-scrollbars", "--default-background-color=00000000",
            f"--force-device-scale-factor={scale:g}",
            f"--window-size={w},{h}",
            f"--screenshot={out}", src,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if not os.path.exists(out):
            raise RuntimeError(
                "chromium failed to rasterize the board: "
                + (proc.stderr.decode("utf-8", "replace")[-500:] or "no output"))
        with open(out, "rb") as fh:
            return fh.read()


async def rasterize_svg_to_png(svg: str, width: int, height: int,
                               *, scale: float = 1.0) -> bytes:
    """Async wrapper for :func:`_rasterize_sync` (runs off the event loop)."""
    return await asyncio.to_thread(_rasterize_sync, svg, width, height, scale=scale)
