"""Render a Board to layered SVG.

Layer stack (bottom -> top), each an Inkscape layer:
  background · grid · region-fill · path · glow · decoration · label · title
Every tile/icon is an individual, class-tagged element so you can hand-edit or
reshape any single piece. Colours come from <defs> gradients so retheming is a
one-line change.
"""
from __future__ import annotations

from . import biomes, icons
from .board import Board, Tile
from .svgcanvas import Canvas

FONT = "Georgia, 'Palatino Linotype', 'Times New Roman', serif"


def _scaled_corners(board: Board, tile: Tile, scale: float):
    cx, cy = tile.x, tile.y
    return [(cx + (px - cx) * scale, cy + (py - cy) * scale)
            for px, py in board.layout.corners(tile.hex)]


def _defs(board: Board) -> list[str]:
    d = []
    # background + vignette
    d.append('<linearGradient id="g-bg" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0" stop-color="#17293f"/>'
             '<stop offset="1" stop-color="#0b1520"/></linearGradient>')
    d.append('<radialGradient id="g-vig" cx="0.5" cy="0.5" r="0.75">'
             '<stop offset="0.55" stop-color="#000000" stop-opacity="0"/>'
             '<stop offset="1" stop-color="#000000" stop-opacity="0.45"/>'
             '</radialGradient>')
    # per-biome vertical bevel gradients
    for b in biomes.BIOMES.values():
        d.append(f'<linearGradient id="g-{b.key}" x1="0" y1="0" x2="0" y2="1">'
                 f'<stop offset="0" stop-color="{b.fill_top}"/>'
                 f'<stop offset="1" stop-color="{b.fill_bottom}"/>'
                 f'</linearGradient>')
    # stone road
    d.append('<linearGradient id="g-stone" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0" stop-color="#aeb4bd"/>'
             '<stop offset="1" stop-color="#6b7079"/></linearGradient>')
    d.append('<linearGradient id="g-stone-rim" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0" stop-color="#d6dae0"/>'
             '<stop offset="1" stop-color="#9aa0a8"/></linearGradient>')
    # reusable glow blur
    d.append('<filter id="f-glow" x="-70%" y="-70%" width="240%" height="240%">'
             '<feGaussianBlur stdDeviation="4.5"/></filter>')
    # parchment for banners
    d.append('<linearGradient id="g-parch" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0" stop-color="#efe0bd"/>'
             '<stop offset="1" stop-color="#d8c194"/></linearGradient>')
    return d


def _hex(board: Board, tile: Tile, fill: str, **kw) -> str:
    pts = board.layout.corners(tile.hex)
    return Canvas.polygon(pts, fill=fill, **kw)


def render(board: Board, *, watermark: str | None = None,
           grid_backdrop: bool | None = None) -> Canvas:
    c = Canvas(board.width, board.height)
    for d in _defs(board):
        c.add_def(d)

    bg = c.layer("background", "Background")
    grid = c.layer("grid", "Grid backdrop")
    fill = c.layer("region-fill", "Region tiles")
    path = c.layer("path", "Path (road)")
    glow = c.layer("glow", "Special glows")
    deco = c.layer("decoration", "Decorations")
    label = c.layer("label", "Region labels")
    title = c.layer("title", "Title & frame")

    # -- background ------------------------------------------------------
    bg.add(c.rect(0, 0, board.width, board.height, fill="url(#g-bg)"))

    # -- faint universe grid (dense "filled" look) -----------------------
    if grid_backdrop is None:
        grid_backdrop = board.style == "filled"
    if grid_backdrop:
        for h in board._universe():
            pts = board.layout.corners(h)
            grid.add(Canvas.polygon(pts, fill="none", stroke="#22344a",
                                    stroke_width=1, opacity=0.5))

    # -- region + path tiles --------------------------------------------
    for t in sorted(board.tiles.values(), key=lambda t: (t.y, t.x)):
        if t.is_path:
            outer = _scaled_corners(board, t, 0.98)
            inner = _scaled_corners(board, t, 0.7)
            g = (Canvas.polygon(outer, fill="url(#g-stone)", stroke="#3c4047",
                                stroke_width=1.5) +
                 Canvas.polygon(inner, fill="url(#g-stone-rim)", stroke="#c7ccd3",
                                stroke_width=1, opacity=0.9))
            path.add(Canvas.group(g, **{"class": f"tile path idx-{t.path_index}",
                                        "id": f"tile-{t.hex.q}-{t.hex.r}"}))
        else:
            fill.add(_hex(board, t, f"url(#g-{t.biome})",
                          stroke=biomes.biome(t.biome).edge, stroke_width=1.5,
                          **{"class": f"tile field {t.biome}",
                             "id": f"tile-{t.hex.q}-{t.hex.r}"}))

    # -- glows -----------------------------------------------------------
    for t in board.tiles.values():
        if not t.glow:
            continue
        col = biomes.GLOW_COLORS.get(t.glow, "#ffffff")
        ring = _scaled_corners(board, t, 1.02)
        glow.add(Canvas.polygon(ring, fill="none", stroke=col, stroke_width=6,
                                filter="url(#f-glow)", opacity=0.9))
        glow.add(Canvas.polygon(_scaled_corners(board, t, 0.94), fill="none",
                                stroke=col, stroke_width=2, opacity=0.95))

    # -- decorations -----------------------------------------------------
    for t in board.tiles.values():
        if t.role == "start":
            deco.add(_flag(t.x, t.y, board.size, "#5fd06a"))
        elif t.role == "finish":
            deco.add(_star(t.x, t.y, board.size, "#ffd35a"))
        elif t.decor:
            deco.add(icons.place(t.decor, t.x, t.y - board.size * 0.05,
                                 board.size))

    # -- region labels ---------------------------------------------------
    if board.labels:
        for reg, lx, ly in board.region_label_points():
            label.add(_banner(reg.name, lx, ly, board.size))

    # -- title + frame ---------------------------------------------------
    title.add(Canvas.rect(6, 6, board.width - 12, board.height - 12,
                          fill="none", stroke="#0a121c", stroke_width=12,
                          rx=4))
    title.add(c.rect(0, 0, board.width, board.height, fill="url(#g-vig)"))
    title.add(_title_scroll(board.title, board.subtitle))
    if watermark:
        title.add(_watermark(board.width, board.height, watermark))

    return c


# ---------------------------------------------------------------------------
# small bespoke marks
# ---------------------------------------------------------------------------

def _flag(x, y, size, color) -> str:
    s = size * 0.9
    pole = f'<line x1="{x-s*0.28}" y1="{y-s*0.5}" x2="{x-s*0.28}" y2="{y+s*0.45}" stroke="#3a2c18" stroke-width="{s*0.09}" stroke-linecap="round"/>'
    flag = (f'<path d="M{x-s*0.28} {y-s*0.5} L{x+s*0.4} {y-s*0.32} '
            f'L{x-s*0.28} {y-s*0.12} Z" fill="{color}" stroke="#20242b" '
            f'stroke-width="{s*0.06}" stroke-linejoin="round"/>')
    return f'<g class="mark start">{pole}{flag}</g>'


def _star(x, y, size, color) -> str:
    import math
    r1, r2 = size * 0.5, size * 0.22
    pts = []
    for i in range(10):
        r = r1 if i % 2 == 0 else r2
        a = math.radians(-90 + i * 36)
        pts.append((x + r * math.cos(a), y + r * math.sin(a)))
    return Canvas.polygon(pts, fill=color, stroke="#8a6d1a",
                          stroke_width=size * 0.06, stroke_linejoin="round",
                          **{"class": "mark finish"})


def _banner(text, x, y, size) -> str:
    fs = max(11.0, size * 0.46)
    w = max(len(text) * fs * 0.56 + fs, size * 3)
    h = fs * 1.7
    bx, by = x - w / 2, y - h / 2
    rect = (f'<rect x="{bx:.1f}" y="{by:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{h*0.28:.1f}" fill="#141f2e" fill-opacity="0.88" '
            f'stroke="#c9b17a" stroke-width="2"/>')
    txt = (f'<text x="{x:.1f}" y="{y + fs*0.34:.1f}" text-anchor="middle" '
           f'font-family="{FONT}" font-size="{fs:.1f}" font-style="italic" '
           f'fill="#f2e6c8" font-weight="bold">{_esc(text)}</text>')
    return f'<g class="region-label">{rect}{txt}</g>'


def _title_scroll(title, subtitle) -> str:
    x, y, w, h = 30, 24, 330, 92
    body = (f'<rect x="{x+14}" y="{y}" width="{w-28}" height="{h}" rx="6" '
            f'fill="url(#g-parch)" stroke="#9c7d45" stroke-width="2"/>')
    roll_l = (f'<rect x="{x}" y="{y-6}" width="16" height="{h+12}" rx="8" '
              f'fill="#b8965a" stroke="#7c6132" stroke-width="2"/>')
    roll_r = (f'<rect x="{x+w-16}" y="{y-6}" width="16" height="{h+12}" rx="8" '
              f'fill="#b8965a" stroke="#7c6132" stroke-width="2"/>')
    t1 = (f'<text x="{x+w/2}" y="{y+44}" text-anchor="middle" '
          f'font-family="{FONT}" font-size="30" font-weight="bold" '
          f'letter-spacing="1" fill="#3a2c14">{_esc(title.upper())}</text>')
    t2 = (f'<text x="{x+w/2}" y="{y+70}" text-anchor="middle" '
          f'font-family="{FONT}" font-size="15" font-style="italic" '
          f'fill="#5c4a24">{_esc(subtitle)}</text>')
    return f'<g class="title-scroll">{body}{roll_l}{roll_r}{t1}{t2}</g>'


def _watermark(w, h, text) -> str:
    marks = []
    step = 230
    for iy in range(-1, int(h / step) + 2):
        for ix in range(-1, int(w / step) + 2):
            px = ix * step + (iy % 2) * step / 2
            py = iy * step + 120
            marks.append(
                f'<text x="{px}" y="{py}" font-family="{FONT}" font-size="34" '
                f'fill="#ffffff" opacity="0.045" transform="rotate(-30 {px} {py})">'
                f'{_esc(text)}</text>')
    return f'<g class="watermark">{"".join(marks)}</g>'


def _esc(s: str) -> str:
    import html
    return html.escape(str(s), quote=True)
