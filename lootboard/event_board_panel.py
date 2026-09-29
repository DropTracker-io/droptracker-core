"""Player-standings panel for the event-wide lootboard.

The normal lootboard template (1074x795, one PNG per theme) has exactly one
table: "Top Looters" with Rank | Username | GP baked into the artwork. An
event board also needs each player's KC and EHE, and there is no room for
either. Rather than draw a new table from scratch (which would clash with
the 80-odd themes groups pick from), this module builds a wider, longer table
out of pieces cut from the theme's OWN Top Looters table, and splices it into
the board between the body and the footer:

    +-----------------------------+
    |  normal board (items, top   |  <- template rows [0, SPLIT_Y)
    |  looters, recent drops)     |
    +-----------------------------+
    |  title bar                  |  <- stretched from the table's title bar
    |  # | Player | Team | KC ... |  <- header cells, text cleaned out
    |  rows ...                   |  <- one clean data row, repeated
    |  bottom frame               |
    +-----------------------------+
    |  footer                     |  <- template rows [SPLIT_Y, 795)
    +-----------------------------+

Every theme shares the same geometry (checked across the style set): column
separators centred on x=65 and x=210, rows every 22px from y=224, the table
frame spanning x 4..296. Interiors are flat, so a one-pixel column sample
tiles cleanly to any width; the only patterned edge (the OSRS style's bead
strips) is tiled at its detected period so the beads stay round.

Pure PIL: no DB, no Redis. :func:`compose` takes rows already shaped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "static/assets/fonts/runescape_uf.ttf"

# --- Template geometry (shared by every theme) ------------------------------
BOARD_W, BOARD_H = 1074, 795
SPLIT_Y = 748           # footer frame starts here
TABLE_L, TABLE_R = 4, 296
TITLE_BAND = (128, 170)
HEADER_BAND = (170, 222)
ROW_BAND = (332, 354)   # a clean data row (rows 0-2 carry "( )" rank art)
BOTTOM_BAND = (486, 506)
FRAME_L = (4, 13)       # table's left frame, x
FRAME_R = (284, 296)    # right frame (+ a little background), x
SEPARATOR = (63, 68)    # a column separator, x
HEADER_SAMPLE_X = 73    # text-free column inside the header cells
ROW_SAMPLE_X = 100      # text-free column inside a data row
TITLE_CAPS = (16, 262)  # title bar: keep [L, caps[0]) and [caps[1], R)
TITLE_MID = (16, 70)    # text-free stretch of the title bar (text starts ~x78)
BG_BAND = (712, 742)    # plain background between the last box and footer
BG_SAMPLE = (20, 300)   # x range of that band free of theme artwork

ROW_H = ROW_BAND[1] - ROW_BAND[0]
PANEL_MARGIN_TOP = 6
PANEL_MARGIN_BOTTOM = 14
PANEL_GAP = 12          # between two side-by-side tables
PANEL_LEFT = TABLE_L
PANEL_RIGHT = 1024      # line up with the item grid / recent-drops boxes


@dataclass
class Column:
    key: str
    label: str
    weight: float           # share of the spare width
    min_w: int
    align: str = "center"   # "left" | "center" | "right"


@dataclass
class PanelRow:
    rank: int
    name: str
    team: Optional[str] = None
    team_color: Optional[Tuple[int, int, int]] = None
    kc: Optional[int] = None
    ehe: Optional[float] = None
    ehe_estimated: bool = False
    loot: int = 0


@dataclass
class PanelSpec:
    title: str
    rows: List[PanelRow]
    show_team: bool = True
    show_effort: bool = True
    note: Optional[str] = None
    more: int = 0                       # players left off the board
    teams: List[Tuple[str, Optional[Tuple[int, int, int]]]] = field(default_factory=list)
    text_color: Tuple[int, int, int] = (255, 255, 0)
    use_gp_colors: bool = True
    columns: List[Column] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Slicing helpers
# --------------------------------------------------------------------------- #

def _detect_period(img: Image.Image, box: Tuple[int, int, int, int],
                   max_period: int = 40) -> int:
    """Horizontal repeat period of a patterned strip (1 when flat)."""
    strip = img.crop(box).convert("L")
    w, h = strip.size
    px = strip.load()
    if w < 4:
        return 1
    flat = max(abs(px[x, y] - px[0, y]) for x in range(w) for y in range(h))
    if flat <= 6:
        return 1
    best, best_err = 1, None
    for p in range(2, min(max_period, w // 2) + 1):
        err = 0
        n = 0
        for x in range(w - p):
            for y in range(0, h, 2):
                err += abs(px[x, y] - px[x + p, y])
                n += 1
        err /= max(1, n)
        if best_err is None or err < best_err - 0.5:
            best, best_err = p, err
    return best


def _tile_h(piece: Image.Image, width: int) -> Image.Image:
    """Repeat ``piece`` horizontally to exactly ``width`` pixels."""
    out = Image.new("RGBA", (max(1, width), piece.height))
    x = 0
    while x < width:
        out.paste(piece, (x, 0))
        x += piece.width
    return out


def _tile_v(piece: Image.Image, height: int) -> Image.Image:
    out = Image.new("RGBA", (piece.width, max(1, height)))
    y = 0
    while y < height:
        out.paste(piece, (0, y))
        y += piece.height
    return out


def _band(template: Image.Image, band: Tuple[int, int]) -> Image.Image:
    return template.crop((0, band[0], template.width, band[1]))


def _patterned_mid(template: Image.Image, band: Tuple[int, int],
                   mid: Tuple[int, int], width: int) -> Image.Image:
    """``width`` px of a strip's middle, tiled at the strip's own period."""
    box = (mid[0], band[0], mid[1], band[1])
    period = _detect_period(template, box)
    if period <= 1:
        piece = template.crop((mid[0], band[0], mid[0] + 1, band[1]))
    else:
        span = max(period, ((mid[1] - mid[0]) // period) * period)
        piece = template.crop((mid[0], band[0], mid[0] + span, band[1]))
    return _tile_h(piece, width)


def _stretch_bar(template: Image.Image, band: Tuple[int, int], width: int,
                 caps: Tuple[int, int], mid: Tuple[int, int]) -> Image.Image:
    """A frame bar (title/bottom) of ``width``: both end caps as drawn, the
    middle tiled."""
    left = template.crop((TABLE_L, band[0], caps[0], band[1]))
    right = template.crop((caps[1], band[0], TABLE_R, band[1]))
    mid_w = max(0, width - left.width - right.width)
    out = Image.new("RGBA", (width, band[1] - band[0]))
    out.paste(left, (0, 0))
    out.paste(_patterned_mid(template, band, mid, mid_w), (left.width, 0))
    out.paste(right, (left.width + mid_w, 0))
    return out


def _cells_row(template: Image.Image, band: Tuple[int, int], sample_x: int,
               widths: Sequence[int]) -> Image.Image:
    """One row of cells of the given interior widths, framed and separated
    with the template's own frame and separator pieces."""
    h = band[1] - band[0]
    frame_l = template.crop((FRAME_L[0], band[0], FRAME_L[1], band[1]))
    frame_r = template.crop((FRAME_R[0], band[0], FRAME_R[1], band[1]))
    sep = template.crop((SEPARATOR[0], band[0], SEPARATOR[1], band[1]))
    col = template.crop((sample_x, band[0], sample_x + 1, band[1]))
    total = frame_l.width + frame_r.width + sum(widths) + sep.width * (len(widths) - 1)
    out = Image.new("RGBA", (total, h))
    x = 0
    out.paste(frame_l, (x, 0))
    x += frame_l.width
    for i, w in enumerate(widths):
        out.paste(_tile_h(col, w), (x, 0))
        x += w
        if i < len(widths) - 1:
            out.paste(sep, (x, 0))
            x += sep.width
    out.paste(frame_r, (x, 0))
    return out


def chrome_width(n_cols: int) -> int:
    """Pixels a table spends on frames + separators."""
    return ((FRAME_L[1] - FRAME_L[0]) + (FRAME_R[1] - FRAME_R[0])
            + (SEPARATOR[1] - SEPARATOR[0]) * (n_cols - 1))


def column_widths(columns: Sequence[Column], table_w: int) -> List[int]:
    """Interior widths filling ``table_w``: every column gets its minimum,
    the rest is shared by weight."""
    inner = table_w - chrome_width(len(columns))
    base = sum(c.min_w for c in columns)
    spare = max(0, inner - base)
    total_w = sum(c.weight for c in columns) or 1
    widths = [c.min_w + int(spare * c.weight / total_w) for c in columns]
    widths[-1] += inner - sum(widths)
    return widths


# --------------------------------------------------------------------------- #
# Background extension
# --------------------------------------------------------------------------- #

def background_extension(template: Image.Image, height: int) -> Image.Image:
    """``height`` px of the board's plain background, outer borders intact."""
    band = template.crop((0, BG_BAND[0], template.width, BG_BAND[1])).convert("RGBA")
    left = band.crop((0, 0, BG_SAMPLE[0], band.height))
    right_x = template.width - BG_SAMPLE[0]
    right = band.crop((right_x, 0, template.width, band.height))
    fill = band.crop((BG_SAMPLE[0], 0, BG_SAMPLE[1], band.height))
    strip = Image.new("RGBA", (template.width, band.height))
    strip.paste(left, (0, 0))
    strip.paste(_tile_h(fill, right_x - BG_SAMPLE[0]), (BG_SAMPLE[0], 0))
    strip.paste(right, (right_x, 0))
    return _tile_v(strip, height)


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #

_BLACK = (0, 0, 0)
_WHITE = (255, 255, 255)
_DIM = (200, 200, 200)
# Top-3 rank numbers, in the colours the template paints its "( )" markers.
_PODIUM = {1: (255, 215, 0), 2: (214, 214, 214), 3: (214, 142, 72)}


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    """``text`` truncated with ".." to fit ``max_w`` pixels."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "..", font=font) > max_w:
        text = text[:-1]
    return (text + "..") if text else ""


def _draw_text(draw, box, text, font, fill, align="center", stroke=1):
    x0, y0, x1, y1 = box
    text = _fit(draw, text, font, x1 - x0 - 8)
    w = draw.textlength(text, font=font)
    asc, desc = font.getmetrics()
    y = y0 + (y1 - y0 - (asc + desc)) / 2 + 1
    if align == "left":
        x = x0 + 6
    elif align == "right":
        x = x1 - 6 - w
    else:
        x = x0 + (x1 - x0 - w) / 2
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=_BLACK)
    return x, w


def format_hours(hours: Optional[float], estimated: bool = False) -> str:
    if hours is None:
        return "-"
    if hours <= 0:
        return "0h"
    txt = f"{hours:.1f}h" if hours < 100 else f"{hours:,.0f}h"
    return ("~" + txt) if estimated else txt


def format_count(n: Optional[int]) -> str:
    if n is None:
        return "-"
    return f"{n:,}" if n < 100_000 else f"{n / 1000:.0f}K"


# --------------------------------------------------------------------------- #
# Table
# --------------------------------------------------------------------------- #

# A team-name column is only worth drawing with this much room to spare over
# every column's minimum; tighter than that, names truncate to a few letters
# and a colour swatch + legend reads better.
TEAM_NAME_SPARE = 150


def team_names_fit(show_team: bool, show_effort: bool, width: int) -> bool:
    if not show_team:
        return False
    cols = _columns(True, show_effort, width < 700, names=True)
    spare = width - chrome_width(len(cols)) - sum(c.min_w for c in cols)
    return spare >= TEAM_NAME_SPARE


def _columns(show_team: bool, show_effort: bool, narrow: bool, *, names: bool) -> List[Column]:
    cols = [Column("rank", "#", 0.0, 34 if narrow else 44)]
    if show_team and not names:
        # A colour swatch per row, keyed by the legend under the title bar.
        cols.append(Column("team", "", 0.0, 22))
    cols.append(Column("name", "Player", 3.0, 120, "left"))
    if show_team and names:
        cols.append(Column("team", "Team", 2.0, 70, "left"))
    if show_effort:
        cols.append(Column("kc", "KC", 1.0, 58))
        cols.append(Column("ehe", "EHE", 1.0, 66))
    cols.append(Column("loot", "Loot", 1.4, 78))
    return cols


def default_columns(show_team: bool, show_effort: bool, width: int) -> List[Column]:
    return _columns(show_team, show_effort, width < 700,
                    names=team_names_fit(show_team, show_effort, width))


def draw_table(template: Image.Image, spec: PanelSpec, rows: Sequence[PanelRow],
               width: int, format_gp, value_color) -> Image.Image:
    """One framed table of ``rows`` at ``width`` px."""
    columns = spec.columns or default_columns(spec.show_team, spec.show_effort, width)
    swatch_only = any(c.key == "team" and not c.label for c in columns)
    widths = column_widths(columns, width)
    header = _cells_row(template, HEADER_BAND, HEADER_SAMPLE_X, widths)
    row_img = _cells_row(template, ROW_BAND, ROW_SAMPLE_X, widths)
    bottom = _stretch_bar(template, BOTTOM_BAND, width, TITLE_CAPS, TITLE_MID)

    header_h = 30  # the template's header is 52px of icon space we don't need
    header = _squash_v(header, header_h)
    n = max(1, len(rows))
    height = header.height + ROW_H * n + bottom.height
    out = Image.new("RGBA", (width, height))
    out.paste(header, (0, 0))
    for i in range(n):
        out.paste(row_img, (0, header.height + i * ROW_H))
    out.paste(bottom, (0, header.height + n * ROW_H))

    draw = ImageDraw.Draw(out)
    head_font = _font(16)
    cell_font = _font(15)
    # x ranges per column
    xs = []
    x = FRAME_L[1] - FRAME_L[0]
    sep_w = SEPARATOR[1] - SEPARATOR[0]
    for w in widths:
        xs.append((x, x + w))
        x += w + sep_w

    for (x0, x1), col in zip(xs, columns):
        _draw_text(draw, (x0, 0, x1, header_h), col.label, head_font,
                   spec.text_color, "left" if col.align == "left" else "center")

    sep_offset = 5  # the separator line at the top of each row tile
    for i, r in enumerate(rows):
        y0 = header_h + i * ROW_H + sep_offset
        y1 = header_h + (i + 1) * ROW_H + 1
        for (x0, x1), col in zip(xs, columns):
            box = (x0, y0 - 2, x1, y1 - 2)
            if col.key == "rank":
                _draw_text(draw, box, str(r.rank), cell_font,
                           _PODIUM.get(r.rank, spec.text_color))
            elif col.key == "name":
                _draw_text(draw, box, r.name, cell_font, spec.text_color, "left")
            elif col.key == "team" and swatch_only:
                if r.team_color:
                    cx, cy = (x0 + x1) // 2, (box[1] + box[3]) // 2
                    draw.rectangle((cx - 5, cy - 5, cx + 5, cy + 5), fill=r.team_color,
                                   outline=_BLACK)
            elif col.key == "team":
                label = r.team or "-"
                sx = x0 + 6
                if r.team_color:
                    cy = (box[1] + box[3]) // 2
                    draw.rectangle((sx, cy - 4, sx + 8, cy + 4), fill=r.team_color,
                                   outline=_BLACK)
                    sx += 13
                _draw_text(draw, (sx - 6, box[1], x1, box[3]), label, cell_font,
                           _WHITE, "left")
            elif col.key == "kc":
                _draw_text(draw, box, format_count(r.kc), cell_font,
                           _WHITE if r.kc else _DIM)
            elif col.key == "ehe":
                _draw_text(draw, box, format_hours(r.ehe, r.ehe_estimated), cell_font,
                           _WHITE if r.ehe else _DIM)
            elif col.key == "loot":
                colour = value_color(r.loot) if spec.use_gp_colors else spec.text_color
                _draw_text(draw, box, format_gp(r.loot), cell_font, colour)
    return out


def _squash_v(img: Image.Image, height: int) -> Image.Image:
    """Shorten a cell row to ``height`` keeping its top and bottom edges
    (frames/bevels live there); the flat middle is simply dropped."""
    if img.height <= height:
        return img
    keep = height // 2
    out = Image.new("RGBA", (img.width, height))
    out.paste(img.crop((0, 0, img.width, keep)), (0, 0))
    out.paste(img.crop((0, img.height - (height - keep), img.width, img.height)), (0, keep))
    return out


def _draw_legend(draw, teams, x0, x1, y, h):
    """Team colour key, centred on one line (names shortened to fit)."""
    font = _font(15)
    teams = list(teams)
    budget = (x1 - x0 - 20) // max(1, len(teams)) - 24
    items = [(_fit(draw, name, font, max(40, budget)), colour) for name, colour in teams]
    widths = [16 + draw.textlength(name, font=font) for name, _ in items]
    gap = 22
    x = x0 + (x1 - x0 - (sum(widths) + gap * (len(items) - 1))) / 2
    cy = y + h // 2
    for (name, colour), w in zip(items, widths):
        if colour:
            draw.rectangle((x, cy - 5, x + 10, cy + 5), fill=colour, outline=_BLACK)
        asc, desc = font.getmetrics()
        draw.text((x + 16, cy - (asc + desc) / 2 + 1), name, font=font, fill=_WHITE,
                  stroke_width=1, stroke_fill=_BLACK)
        x += w + gap


# --------------------------------------------------------------------------- #
# Compose
# --------------------------------------------------------------------------- #

SPLIT_THRESHOLD = 14    # more rows than this -> two tables side by side
MAX_ROWS = 40           # players drawn at most (two columns of 20)


def compose(board: Image.Image, template: Image.Image, spec: PanelSpec,
            format_gp, value_color) -> Image.Image:
    """The finished board: ``board`` (already drawn) with the player panel
    spliced in above its footer."""
    template = template.convert("RGBA")
    board = board.convert("RGBA")
    rows = list(spec.rows[:MAX_ROWS])
    total_w = PANEL_RIGHT - PANEL_LEFT

    title = _stretch_bar(template, TITLE_BAND, total_w, TITLE_CAPS, TITLE_MID)
    if len(rows) > SPLIT_THRESHOLD:
        half = (len(rows) + 1) // 2
        w = (total_w - PANEL_GAP) // 2
        tables = [draw_table(template, spec, rows[:half], w, format_gp, value_color),
                  draw_table(template, spec, rows[half:], w, format_gp, value_color)]
        xs = [PANEL_LEFT, PANEL_LEFT + w + PANEL_GAP]
    else:
        tables = [draw_table(template, spec, rows, total_w, format_gp, value_color)]
        xs = [PANEL_LEFT]

    table_w = (total_w - PANEL_GAP) // 2 if len(tables) > 1 else total_w
    swatches = spec.show_team and not team_names_fit(spec.show_team, spec.show_effort, table_w)
    legend_h = 24 if (swatches and spec.teams) else 0
    note_h = 22 if (spec.note or spec.more) else 0
    body_h = max(t.height for t in tables)
    panel_h = (PANEL_MARGIN_TOP + title.height + legend_h + body_h + note_h
               + PANEL_MARGIN_BOTTOM)
    ext = background_extension(template, panel_h)
    ext.paste(title, (PANEL_LEFT, PANEL_MARGIN_TOP), title)
    body_y = PANEL_MARGIN_TOP + title.height + legend_h
    for t, x in zip(tables, xs):
        ext.paste(t, (x, body_y), t)

    draw = ImageDraw.Draw(ext)
    if legend_h:
        _draw_legend(draw, spec.teams, PANEL_LEFT, PANEL_LEFT + total_w,
                     PANEL_MARGIN_TOP + title.height + 2, legend_h - 2)
    _draw_text(draw, (PANEL_LEFT, PANEL_MARGIN_TOP + 2, PANEL_LEFT + total_w,
                      PANEL_MARGIN_TOP + title.height),
               spec.title, _font(22), spec.text_color, stroke=2)
    if note_h:
        bits = []
        if spec.more:
            bits.append(f"+{spec.more} more player{'s' if spec.more != 1 else ''}")
        if spec.note:
            bits.append(spec.note)
        y = body_y + body_h + 2
        _draw_text(draw, (PANEL_LEFT, y, PANEL_LEFT + total_w, y + note_h),
                   "  |  ".join(bits), _font(15), _DIM)

    out = Image.new("RGBA", (board.width, board.height + panel_h))
    out.paste(board.crop((0, 0, board.width, SPLIT_Y)), (0, 0))
    out.paste(ext, (0, SPLIT_Y))
    out.paste(board.crop((0, SPLIT_Y, board.width, board.height)), (0, SPLIT_Y + panel_h))
    return out.convert("RGB")
