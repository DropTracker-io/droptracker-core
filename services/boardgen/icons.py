"""Procedural vector glyphs for tile decorations.

Each drawer authors its art inside a 100x100 box (icon centred near 50,50) and
returns raw SVG markup. `place()` wraps a glyph with a translate+scale so it can
be dropped at any tile at any size; because the whole group is scaled, stroke
weights stay proportional. Swap these for real sprite <image> tags via
assets.py without touching the generator.
"""
from __future__ import annotations

# Common outline for the flat game-art look.
OUT = dict(stroke="#20242b", stroke_width=4, stroke_linejoin="round",
           stroke_linecap="round")


def _p(d, **kw):
    a = {**OUT, **kw}
    at = " ".join(f'{k.replace("_","-")}="{v}"' for k, v in a.items())
    return f'<path d="{d}" {at}/>'


def _c(cx, cy, r, **kw):
    a = {**OUT, **kw}
    at = " ".join(f'{k.replace("_","-")}="{v}"' for k, v in a.items())
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" {at}/>'


def _line(x1, y1, x2, y2, **kw):
    a = {**OUT, **kw}
    at = " ".join(f'{k.replace("_","-")}="{v}"' for k, v in a.items())
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" {at}/>'


# ---------------------------------------------------------------------------

def tree():
    return (
        _p("M43 66 h14 v18 h-14 Z", fill="#7a4a24") +
        _p("M50 46 L26 78 L74 78 Z", fill="#2f7a34") +
        _p("M50 32 L30 60 L70 60 Z", fill="#3a8c3f") +
        _p("M50 18 L36 44 L64 44 Z", fill="#46a049"))


def snowtree():
    return (
        _p("M43 66 h14 v18 h-14 Z", fill="#6f6a63") +
        _p("M50 46 L26 78 L74 78 Z", fill="#dfe9f2") +
        _p("M50 32 L30 60 L70 60 Z", fill="#eef4fa") +
        _p("M50 18 L36 44 L64 44 Z", fill="#ffffff"))


def deadtree():
    return (
        _p("M45 86 C 47 62 43 50 45 30 L55 30 C 57 50 53 62 55 86 Z",
           fill="#6a5236") +
        _line(50, 44, 32, 28) + _line(50, 54, 68, 38) + _line(50, 34, 60, 20) +
        _line(50, 62, 36, 52))


def mushroom():
    return (
        _p("M43 54 h14 v22 q-7 6 -14 0 Z", fill="#f0e2c0") +
        _p("M22 54 C 22 30 78 30 78 54 Z", fill="#c33f34") +
        _c(38, 44, 4, fill="#ffffff", stroke="none") +
        _c(56, 40, 5, fill="#ffffff", stroke="none") +
        _c(64, 48, 3, fill="#ffffff", stroke="none"))


def anvil():
    return (
        _p("M20 40 L74 40 L68 50 L52 50 L52 56 L40 56 L40 50 L30 50 "
           "C 26 50 22 46 20 40 Z", fill="#565b63") +
        _p("M37 56 L55 56 L62 74 L30 74 Z", fill="#42474e"))


def ankh():
    return (
        f'<ellipse cx="50" cy="30" rx="12" ry="15" fill="none" '
        f'stroke="#e8c552" stroke-width="8"/>' +
        _line(50, 42, 50, 82, stroke="#e8c552", stroke_width=8) +
        _line(33, 54, 67, 54, stroke="#e8c552", stroke_width=8))


def cactus():
    return (
        _p("M44 86 L44 42 Q44 36 50 36 Q56 36 56 42 L56 86 Z", fill="#3f8f43") +
        _p("M44 58 Q34 58 34 48 L34 44 Q34 39 39 44 L39 48 Q39 52 44 52 Z",
           fill="#3f8f43") +
        _p("M56 64 Q66 64 66 54 L66 50 Q66 45 61 50 L61 54 Q61 58 56 58 Z",
           fill="#3f8f43"))


def skull():
    return (
        _c(50, 44, 22, fill="#eae3d0") +
        _p("M34 56 L66 56 L62 74 C 56 80 44 80 38 74 Z", fill="#eae3d0") +
        _c(42, 46, 6, fill="#20242b", stroke="none") +
        _c(58, 46, 6, fill="#20242b", stroke="none") +
        _p("M50 54 L46 62 L54 62 Z", fill="#20242b", stroke="none"))


def chest():
    return (
        _p("M28 50 h44 v24 h-44 Z", fill="#7a4a24") +
        _p("M28 50 C 28 34 72 34 72 50 Z", fill="#95602f") +
        _line(50, 36, 50, 74, stroke="#e8c552", stroke_width=5) +
        _p("M46 56 h8 v10 h-8 Z", fill="#e8c552"))


def crystal():
    return (
        _p("M50 16 L66 42 L50 86 L34 42 Z", fill="#8fc3e6") +
        _p("M50 16 L50 86", fill="none", stroke="#d6ecfb", stroke_width=4) +
        _p("M34 42 L66 42", fill="none", stroke="#d6ecfb", stroke_width=4))


def fossil():
    g = ('<g transform="rotate(-28 50 50)">' +
         _c(32, 44, 7, fill="#eae3d0") + _c(32, 56, 7, fill="#eae3d0") +
         _c(68, 44, 7, fill="#eae3d0") + _c(68, 56, 7, fill="#eae3d0") +
         '<rect x="32" y="45" width="36" height="10" rx="3" '
         'fill="#eae3d0" stroke="#20242b" stroke-width="4"/></g>')
    return g


DRAWERS = {
    "tree": tree, "snowtree": snowtree, "deadtree": deadtree,
    "mushroom": mushroom, "anvil": anvil, "ankh": ankh, "cactus": cactus,
    "skull": skull, "chest": chest, "crystal": crystal, "fossil": fossil,
}


# Optional real-sprite library (see assets.py). When set, any icon it knows
# about is emitted as an <image> instead of the vector glyph.
_SPRITES = None


def set_sprite_library(lib) -> None:
    global _SPRITES
    _SPRITES = lib


def place(name: str, x: float, y: float, size: float,
          rotate: float = 0.0, opacity: float = 1.0) -> str:
    """Return the named glyph, centred on (x, y), fitted to `size` px."""
    if _SPRITES is not None and _SPRITES.has(name):
        op = "" if opacity >= 1.0 else f' opacity="{opacity:.2f}"'
        rot = f' transform="rotate({rotate:.1f} {x:.2f} {y:.2f})"' if rotate else ""
        return (f'<image class="icon icon-{name}"{op}{rot} '
                f'href="{_SPRITES.href(name)}" x="{x-size/2:.2f}" '
                f'y="{y-size/2:.2f}" width="{size:.2f}" height="{size:.2f}"/>')
    drawer = DRAWERS.get(name)
    if drawer is None:
        return ""
    s = size / 100.0
    op = "" if opacity >= 1.0 else f' opacity="{opacity:.2f}"'
    rot = f" rotate({rotate:.1f} 50 50)" if rotate else ""
    return (f'<g class="icon icon-{name}"{op} '
            f'transform="translate({x:.2f} {y:.2f}) scale({s:.4f}){rot} '
            f'translate(-50 -50)">{drawer()}</g>')


def available() -> list[str]:
    return sorted(DRAWERS)
