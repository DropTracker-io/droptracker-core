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
# "grid" (2026-09): the numbered Chutes & Ladders board — a boustrophedon
# grid that starts bottom-left, rendered by build_grid_assets (no hex engine).
STYLES = ("path", "filled", "grid")
GRID_COLS = 10
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
    # Only ever grow past the base canvas: the base fits ~54 movable tiles, and
    # smaller targets are reached by trimming/padding the road on it. Shrinking
    # for tiny targets left no room for regions and isn't needed.
    factor = max(1.0, (tiles / _BASE_TILES) ** 0.5)
    return round(_BASE_W * factor), round(_BASE_H * factor)


def _difficulty_for(i: int) -> str:
    """air -> water -> earth -> fire, repeating (the designer's cycle)."""
    return EVENT_TASK_DIFFICULTIES[i % len(EVENT_TASK_DIFFICULTIES)]


def build_board(p: GenParams) -> Board:
    """Generate the boardgen Board for these params (pure, seedable).

    ``p.tiles`` is an EXACT movable-tile target: ``target_tiles`` pads/trims the
    road to that count on the sized canvas, and when the road runs out of
    reachable room (fragmented pockets / large targets) the canvas is grown a
    few times until the count is hit — so the generated board always has exactly
    ``p.tiles`` path tiles, matching what the admin typed."""
    width, height = _canvas_for(p.tiles)
    board = Board.generate(
        width=width, height=height, seed=p.seed, style=p.style,
        regions=p.regions, title=p.title, subtitle=p.subtitle,
        target_tiles=p.tiles,
    )
    tries = 0
    while len(board.path) < p.tiles and tries < 10:
        width, height, tries = round(width * 1.12), round(height * 1.12), tries + 1
        board = Board.generate(
            width=width, height=height, seed=p.seed, style=p.style,
            regions=p.regions, title=p.title, subtitle=p.subtitle,
            target_tiles=p.tiles,
        )
    return board


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


# --------------------------------------------------------------------------- #
# Numbered grid (the Chutes & Ladders board)
# --------------------------------------------------------------------------- #
_GRID_CELL = 120
_GRID_MARGIN = 60
_GRID_TITLE_BAND = 150
_GRID_FONT = "Georgia, 'Palatino Linotype', 'Times New Roman', serif"


def grid_layout(count: int, cols: int = GRID_COLS) -> list[dict]:
    """The classic boustrophedon: tile 0 bottom-left, the row above runs the
    other way, and so on up — the way a Chutes & Ladders board reads. Returns
    ``[{idx, col, row}]`` with ``row`` 0 = bottom."""
    out = []
    for i in range(count):
        row, within = divmod(i, cols)
        col = within if row % 2 == 0 else cols - 1 - within
        out.append({"idx": i, "col": col, "row": row})
    return out


def build_grid_assets(p: GenParams) -> dict:
    """The numbered-grid board: ``p.tiles`` cells in a ``GRID_COLS``-wide
    boustrophedon, every cell printed with the SAME idx the site shows ("S",
    1..N-2, "F") so the art and the overlay never disagree. The chutes and
    ladders themselves are NOT baked in — they are tile links the board view
    draws live, so a coordinator can add or move one without regenerating.
    Pure (no I/O): the same {svg, tiles, width, height, meta} shape as the hex
    generator."""
    from services.boardgen.svgcanvas import Canvas

    cols = GRID_COLS
    n = int(p.tiles)
    rows = max(1, -(-n // cols))
    cell, margin, band = _GRID_CELL, _GRID_MARGIN, _GRID_TITLE_BAND
    width = margin * 2 + cols * cell
    height = margin * 2 + band + rows * cell

    c = Canvas(width, height)
    c.add_def('<linearGradient id="g-bg" x1="0" y1="0" x2="0" y2="1">'
              '<stop offset="0" stop-color="#1b2433"/>'
              '<stop offset="1" stop-color="#0c131c"/></linearGradient>')
    c.add_def('<linearGradient id="g-parch" x1="0" y1="0" x2="0" y2="1">'
              '<stop offset="0" stop-color="#efe0bd"/>'
              '<stop offset="1" stop-color="#d8c194"/></linearGradient>')
    c.add_def('<linearGradient id="g-cell-a" x1="0" y1="0" x2="0" y2="1">'
              '<stop offset="0" stop-color="#e9dcbb"/>'
              '<stop offset="1" stop-color="#cdb98a"/></linearGradient>')
    c.add_def('<linearGradient id="g-cell-b" x1="0" y1="0" x2="0" y2="1">'
              '<stop offset="0" stop-color="#d9c9a0"/>'
              '<stop offset="1" stop-color="#bda678"/></linearGradient>')
    c.add_def('<linearGradient id="g-cell-end" x1="0" y1="0" x2="0" y2="1">'
              '<stop offset="0" stop-color="#f3d67a"/>'
              '<stop offset="1" stop-color="#c9a23c"/></linearGradient>')

    bg = c.layer("background", "Background")
    grid = c.layer("grid", "Grid cells")
    numbers = c.layer("label", "Numbers")
    title = c.layer("title", "Title & frame")

    bg.add(c.rect(0, 0, width, height, fill="url(#g-bg)"))
    # Board frame behind the cells.
    bg.add(c.rect(margin - 14, margin + band - 14, cols * cell + 28, rows * cell + 28,
                  rx=10, fill="#2b2118", stroke="#9c7d45", stroke_width=4))

    last = n - 1
    tiles: list[dict] = []
    for spot in grid_layout(n, cols):
        i, col, row = spot["idx"], spot["col"], spot["row"]
        x = margin + col * cell
        y = margin + band + (rows - 1 - row) * cell
        end = i == 0 or i == last
        fill = ("url(#g-cell-end)" if end
                else "url(#g-cell-a)" if (row + col) % 2 == 0 else "url(#g-cell-b)")
        grid.add(c.rect(x + 3, y + 3, cell - 6, cell - 6, rx=8, fill=fill,
                        stroke="#7c6132", stroke_width=2,
                        **{"class": f"cell cell-{i}", "id": f"cell-{i}"}))
        label = "S" if i == 0 else "F" if i == last else str(i)
        numbers.add(c.text(
            x + cell / 2, y + cell / 2 + (cell * 0.16 if not end else cell * 0.2),
            label, text_anchor="middle", font_family=_GRID_FONT,
            font_size=cell * (0.5 if end else 0.36), font_weight="bold",
            fill="#3a2c14", **{"class": "cell-number"}))
        tiles.append({
            "idx": i,
            "x": round((x + cell / 2) / width, 4),
            "y": round((y + cell / 2) / height, 4),
            "difficulty": _difficulty_for(i),
            "tile_kind": "start" if i == 0 else "finish" if i == last else "normal",
        })

    # Title scroll across the top band (the hex renderer's parchment look).
    tx, ty, tw, th = margin, 30, cols * cell, 96
    title.add(c.rect(tx + 14, ty, tw - 28, th, rx=6, fill="url(#g-parch)",
                     stroke="#9c7d45", stroke_width=2))
    title.add(c.rect(tx, ty - 6, 16, th + 12, rx=8, fill="#b8965a", stroke="#7c6132",
                     stroke_width=2))
    title.add(c.rect(tx + tw - 16, ty - 6, 16, th + 12, rx=8, fill="#b8965a",
                     stroke="#7c6132", stroke_width=2))
    title.add(c.text(tx + tw / 2, ty + 46, p.title.upper(), text_anchor="middle",
                     font_family=_GRID_FONT, font_size=34, font_weight="bold",
                     letter_spacing=1, fill="#3a2c14"))
    title.add(c.text(tx + tw / 2, ty + 76, p.subtitle, text_anchor="middle",
                     font_family=_GRID_FONT, font_size=16, font_style="italic",
                     fill="#5c4a24"))
    if p.watermark:
        title.add(c.text(width - margin, height - 18, p.watermark, text_anchor="end",
                         font_family=_GRID_FONT, font_size=18, fill="#c9b17a",
                         fill_opacity=0.7))

    return {
        "svg": c.render(),
        "tiles": tiles,
        "width": int(width),
        "height": int(height),
        "meta": {
            "seed": p.seed,
            "style": "grid",
            "regions": 0,
            "rows": rows,
            "cols": cols,
            "path_tiles": n,
            "total_tiles": n,
            "skipped_regions": 0,
        },
    }


def build_board_assets(p: GenParams) -> dict:
    """The offline half: Board -> {svg, tiles, width, height, meta}. No I/O, so
    a unit test can exercise the whole mapping without B2 or chromium."""
    if p.style == "grid":
        return build_grid_assets(p)
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


def standings_scroll_svg(standings: list | None, width: int, height: int) -> str:
    """Render just the ``<g class="standings-scroll">…</g>`` fragment for the
    given live standings, to splice into a stored board SVG at PNG-export time.

    The generate-time background SVG is deliberately standings-free (the web app
    overlays its own live banner; baking would freeze/duplicate it). The board
    PNG export injects CURRENT standings here instead. Returns "" when there is
    nothing to show or on any render error, so the caller can no-op safely."""
    if not standings:
        return ""
    try:
        from services.boardgen.render import _standings_scroll
        return _standings_scroll(standings, int(width or 0), int(height or 0))
    except Exception:
        return ""


async def upload_board_svg(svg: str, event_id: int, seed: int) -> str:
    """Publish the generated SVG to B2 and return its public CDN URL (served as
    the tile-overlay background). Mirrors the board-background upload path."""
    from utils.b2_storage import upload_bytes

    # Same env read as web_api.routes.submissions.B2_CDN_BASE_URL — inlined so
    # this worker-side path never imports the web_api package (fragile across
    # deploys in a long-lived process; see services/event_lifecycle.py).
    B2_CDN_BASE_URL = os.getenv("B2_CDN_BASE_URL", "https://videos.droptracker.io")

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
        profile = os.path.join(tmp, "profile")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(svg)
        cmd = [
            _CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage",
            # Keep the whole profile inside the temp dir so chromium never
            # touches the caller's real HOME (see the env override below).
            f"--user-data-dir={profile}",
            "--hide-scrollbars", "--default-background-color=00000000",
            f"--force-device-scale-factor={scale:g}",
            f"--window-size={w},{h}",
            f"--screenshot={out}", src,
        ]
        # Headless chromium needs a WRITABLE $HOME for its profile + crashpad
        # database; without it the crashpad handler aborts with "--database is
        # required" and no screenshot is produced. The core bot runs under
        # systemd ProtectHome=true, so the inherited HOME is inaccessible —
        # point HOME at the private temp dir (writable under PrivateTmp) so the
        # rasterizer works from any service, not just the webapi's user.
        env = {**os.environ, "HOME": tmp}
        proc = subprocess.run(cmd, capture_output=True, timeout=60, env=env)
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
